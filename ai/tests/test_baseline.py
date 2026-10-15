"""GPU-free contract checks; these do not prove real GPU inference."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "baseline", Path(__file__).resolve().parents[1] / "src" / "baseline.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model_path = self.root / "model"
        self.model_path.mkdir()
        (self.model_path / "config.json").write_text("{}")
        self.config = self.root / "baseline.json"
        self.config.write_text(json.dumps({"system_prompt": "system", "max_new_tokens": 160}))
        self.settings = baseline.read_settings(self.config, "synthetic question", str(self.model_path))

    def fake_runtime(self):
        torch = ModuleType("torch")
        torch.bfloat16 = "BF16"
        torch.cuda = SimpleNamespace(is_available=Mock(return_value=True),
                                    set_device=Mock(), is_bf16_supported=Mock(return_value=True))
        torch.inference_mode = nullcontext
        parameter = SimpleNamespace(device=SimpleNamespace(type="cuda", index=0),
                                    dtype="BF16", is_floating_point=lambda: True)
        model = Mock()
        model.parameters.return_value = [parameter]
        model.buffers.return_value = []
        model.config.max_position_embeddings = 32768
        model.generation_config.eos_token_id = [1, 2]
        outputs = Mock()
        outputs.__getitem__ = Mock(return_value=[8, 9])
        model.generate.return_value = outputs
        inputs = {"input_ids": SimpleNamespace(shape=(1, 5)), "attention_mask": "mask"}

        class Inputs(dict):
            def to(self, device):
                self.device = device
                return self

        tokenizer = Mock()
        tokenizer.eos_token_id = 2
        tokenizer.apply_chat_template.return_value = Inputs(inputs)
        tokenizer.decode.return_value = "synthetic answer"
        transformers = ModuleType("transformers")
        transformers.AutoTokenizer = SimpleNamespace(from_pretrained=Mock(return_value=tokenizer))
        transformers.AutoModelForCausalLM = SimpleNamespace(from_pretrained=Mock(return_value=model))
        transformers.GenerationConfig = Mock(side_effect=lambda **kw: SimpleNamespace(**kw))
        return torch, transformers, tokenizer, model, outputs

    def run_fake(self, runtime):
        torch, transformers, *_ = runtime
        with patch.object(baseline, "check_environment"), patch.dict(
                baseline.sys.modules, {"torch": torch, "transformers": transformers}), patch.dict(
                baseline.os.environ, {}, clear=True):
            answer = baseline.infer(self.settings)
            self.assertEqual(baseline.os.environ["HF_HUB_OFFLINE"], "1")
            return answer

    def test_greedy_offline_loading_and_new_token_slice(self):
        runtime = self.fake_runtime()
        torch, tf, tokenizer, model, outputs = runtime
        self.assertEqual(self.run_fake(runtime), "synthetic answer")
        tf.AutoTokenizer.from_pretrained.assert_called_once_with(
            str(self.model_path.resolve()), local_files_only=True, trust_remote_code=False)
        tf.AutoModelForCausalLM.from_pretrained.assert_called_once_with(
            str(self.model_path.resolve()), dtype="BF16", device_map={"": 0}, local_files_only=True,
            trust_remote_code=False, use_safetensors=True, attn_implementation="eager")
        torch.cuda.set_device.assert_called_once_with(0)
        torch.cuda.is_bf16_supported.assert_called_once_with(including_emulation=False)
        messages = tokenizer.apply_chat_template.call_args.args[0]
        self.assertEqual(messages, [{"role": "system", "content": "system"},
                                    {"role": "user", "content": "synthetic question"}])
        self.assertEqual(tokenizer.apply_chat_template.call_args.kwargs,
                         dict(tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt"))
        self.assertEqual(tokenizer.apply_chat_template.return_value.device, "cuda:0")
        self.assertEqual(model.generate.call_args.kwargs["attention_mask"], "mask")
        generation = model.generate.call_args.kwargs["generation_config"]
        self.assertFalse(generation.do_sample)
        self.assertEqual(generation.max_new_tokens, 160)
        self.assertEqual(generation.eos_token_id, [1, 2])
        self.assertEqual(generation.pad_token_id, 2)
        outputs.__getitem__.assert_called_once_with((0, slice(5, None)))
        tokenizer.decode.assert_called_once_with([8, 9], skip_special_tokens=True)

    def test_gpu_failures_before_model_loading(self):
        for code in ("CUDA", "BF16"):
            with self.subTest(code=code):
                runtime = self.fake_runtime()
                target = runtime[0].cuda.is_available if code == "CUDA" else runtime[0].cuda.is_bf16_supported
                target.return_value = False
                with self.assertRaises(baseline.BaselineError) as ctx:
                    self.run_fake(runtime)
                self.assertEqual(ctx.exception.code, code)
                runtime[1].AutoModelForCausalLM.from_pretrained.assert_not_called()

    def test_no_cpu_or_fp16_parameters_or_buffers(self):
        for kind in ("cpu_parameter", "cpu_buffer", "fp16_parameter", "empty_model"):
            with self.subTest(kind=kind):
                runtime = self.fake_runtime()
                model = runtime[3]
                if kind == "empty_model":
                    model.parameters.return_value = []
                elif kind == "cpu_buffer":
                    model.buffers.return_value = [SimpleNamespace(device=SimpleNamespace(type="cpu", index=None))]
                elif kind == "cpu_parameter":
                    model.parameters.return_value[0].device.type = "cpu"
                else:
                    model.parameters.return_value[0].dtype = "FP16"
                with self.assertRaises(baseline.BaselineError) as ctx:
                    self.run_fake(runtime)
                self.assertEqual(ctx.exception.code, "PLACEMENT")
                model.generate.assert_not_called()

    def test_context_overflow_and_template_failure(self):
        for kind in ("length", "template"):
            runtime = self.fake_runtime()
            if kind == "length":
                runtime[3].config.max_position_embeddings = 164
            else:
                runtime[2].apply_chat_template.side_effect = ValueError("private prompt")
            with self.assertRaises(baseline.BaselineError) as ctx:
                self.run_fake(runtime)
            self.assertEqual(ctx.exception.code, "INPUT")
            runtime[3].generate.assert_not_called()

    def test_environment_checks(self):
        with patch.object(baseline.sys, "version_info", (3, 12, 7)), patch.object(
                baseline.sys, "prefix", "venv"), patch.object(baseline.sys, "base_prefix", "base"):
            with patch.dict(baseline.os.environ, {}, clear=True):
                with self.assertRaises(baseline.BaselineError) as ctx:
                    baseline.check_environment()
                self.assertEqual(ctx.exception.code, "ALLOCATION")
            with patch.dict(baseline.os.environ, {"SLURM_JOB_ID": "123"}, clear=True):
                baseline.check_environment()
                with patch.object(baseline.sys, "prefix", "base"):
                    with self.assertRaises(baseline.BaselineError) as ctx:
                        baseline.check_environment()
                    self.assertEqual(ctx.exception.code, "ENVIRONMENT")
        with patch.object(baseline.sys, "version_info", (3, 14)):
            with self.assertRaises(baseline.BaselineError):
                baseline.check_environment()

    def test_token_boundaries_and_bad_config(self):
        for limit in (1, 1024):
            self.assertEqual(baseline.read_settings(self.config, "q", str(self.model_path), limit)["max_new_tokens"], limit)
        for limit in (0, 1025, True, "160"):
            with self.assertRaises(baseline.BaselineError):
                baseline.read_settings(self.config, "q", str(self.model_path), limit)
        for prompt in ("", " ", "q" * 16001):
            with self.assertRaises(baseline.BaselineError):
                baseline.read_settings(self.config, prompt, str(self.model_path))
        self.config.write_text("not json")
        with self.assertRaises(baseline.BaselineError):
            baseline.read_settings(self.config, "q", str(self.model_path))

    def test_missing_model_does_not_fall_back_to_hub_id(self):
        for path in (None, "Qwen/Qwen2.5-7B-Instruct", str(self.root / "missing")):
            with self.assertRaises(baseline.BaselineError) as ctx:
                baseline.read_settings(self.config, "q", path)
            self.assertEqual(ctx.exception.code, "MODEL_PATH")

    def test_sensitive_exceptions_are_not_printed(self):
        for phase in ("load", "template", "generate"):
            runtime = self.fake_runtime()
            target = {"load": runtime[1].AutoModelForCausalLM.from_pretrained,
                      "template": runtime[2].apply_chat_template,
                      "generate": runtime[3].generate}[phase]
            target.side_effect = RuntimeError("SECRET private path private prompt")
            out, err = io.StringIO(), io.StringIO()
            with patch.object(baseline, "check_environment"), patch.dict(
                    baseline.sys.modules, {"torch": runtime[0], "transformers": runtime[1]}), patch.dict(
                    baseline.os.environ, {}, clear=True), patch.object(
                    baseline.sys, "stdin", io.StringIO("synthetic question")), redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(baseline.main(["--model-path", str(self.model_path), "--config", str(self.config)]), 1)
            self.assertNotIn("SECRET", err.getvalue())
            self.assertNotIn("private", err.getvalue())
            self.assertNotIn(str(self.model_path), err.getvalue())
            self.assertEqual(out.getvalue(), "")


    def test_argument_errors_are_safe_and_help_is_preserved(self):
        for argv in (["--max-new-tokens", "TOP_SECRET_TO_ECHO"],
                     ["--model-path"], ["--unknown-key", "TOP_SECRET_TO_ECHO"]):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err), patch.object(
                        baseline.sys, "stdin", Mock()) as stdin, patch.object(baseline, "infer") as infer:
                    self.assertEqual(baseline.main(argv), 1)
                    stdin.read.assert_not_called()
                    infer.assert_not_called()
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(err.getvalue(), f"[CONFIG] {baseline.MESSAGES['CONFIG']}\n")
                self.assertNotIn("TOP_SECRET_TO_ECHO", err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                baseline.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--model-path", out.getvalue())
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
