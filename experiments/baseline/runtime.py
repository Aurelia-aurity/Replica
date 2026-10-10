"""Resident adapter matching #13 loading/template/greedy policy."""
import gc
import os
import sys
import time
import importlib.metadata
from pathlib import Path
from common import EvaluationError, canonical, digest, validate_success

class Runtime:
    execution_kind = 'school_gpu_user_run'

    def __init__(self, model_path, config):
        if sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix:
            raise EvaluationError('ENVIRONMENT', fatal=True)
        if not os.environ.get('SLURM_JOB_ID', '').strip():
            raise EvaluationError('ALLOCATION', fatal=True)
        path = Path(model_path).expanduser().resolve(strict=True)
        if not path.is_dir() or not (path / 'config.json').is_file():
            raise EvaluationError('MODEL_PATH', fatal=True)
        os.environ['HF_HUB_OFFLINE'] = '1'
        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig
        except Exception:
            raise EvaluationError('DEPENDENCY', fatal=True) from None
        self.torch, self.GenerationConfig, self.config = torch, GenerationConfig, config
        try:
            if not torch.cuda.is_available():
                raise EvaluationError('CUDA', fatal=True)
            torch.cuda.set_device(0)
            if not torch.cuda.is_bf16_supported(including_emulation=False):
                raise EvaluationError('BF16', fatal=True)
            torch.cuda.synchronize(0)
            start = time.perf_counter_ns()
            self.tokenizer = AutoTokenizer.from_pretrained(
                str(path), local_files_only=True, trust_remote_code=False)
            self.model = AutoModelForCausalLM.from_pretrained(
                str(path), dtype=torch.bfloat16, device_map={'': 0},
                local_files_only=True, trust_remote_code=False,
                use_safetensors=True, attn_implementation='eager')
            self.model.eval()
            torch.cuda.synchronize(0)
            self.load_ms = (time.perf_counter_ns() - start) / 1e6
            tensors = list(self.model.parameters()) + list(self.model.buffers())
            if (not tensors or any(t.device.type != 'cuda' or t.device.index != 0 for t in tensors)
                or any(t.is_floating_point() and t.dtype != torch.bfloat16
                       for t in self.model.parameters())):
                raise EvaluationError('PLACEMENT', fatal=True)
            props = torch.cuda.get_device_properties(0)
            self.environment = {
                'python': '.'.join(map(str, sys.version_info[:3])),
                'packages': {p: importlib.metadata.version(p) for p in
                             ('torch', 'transformers', 'accelerate', 'safetensors')},
                'torch_cuda_build': torch.version.cuda,
                'gpu_name': props.name, 'gpu_total_memory_bytes': props.total_memory,
                'gpu_capability': list(torch.cuda.get_device_capability(0)),
                'dtype': 'bfloat16', 'device': 'cuda:0', 'attention': 'eager',
                'cache_policy': 'generation_default_fresh_per_call',
                'resident_allocated_bytes': torch.cuda.memory_allocated(0),
                'resident_reserved_bytes': torch.cuda.memory_reserved(0),
                'model_revision': None, 'model_revision_status': 'unverified',
                'driver_version': None,
            }
        except EvaluationError:
            raise
        except Exception:
            raise EvaluationError('LOAD', fatal=True) from None

    def _health(self, oom):
        try:
            gc.collect()
            if oom:
                self.torch.cuda.empty_cache()
            self.torch.cuda.synchronize(0)
            probe = self.torch.empty(1, device='cuda:0')
            self.torch.cuda.synchronize(0)
            del probe
            return True
        except Exception:
            return False

    def generate(self, prompt, cap):
        torch = self.torch
        inputs = outputs = generated = None
        error = None
        try:
            try:
                inputs = self.tokenizer.apply_chat_template(
                    [{'role': 'system', 'content': self.config['system_prompt']},
                     {'role': 'user', 'content': prompt}], tokenize=True,
                    add_generation_prompt=True, return_dict=True, return_tensors='pt')
                input_count = inputs['input_ids'].shape[1]
                if input_count + cap > self.model.config.max_position_embeddings:
                    raise EvaluationError('INPUT')
                generation = self.GenerationConfig(
                    max_new_tokens=cap, do_sample=False,
                    eos_token_id=self.model.generation_config.eos_token_id,
                    pad_token_id=self.tokenizer.eos_token_id)
            except EvaluationError:
                raise
            except Exception:
                raise EvaluationError('INPUT') from None
            try:
                inputs = inputs.to('cuda:0')
                torch.cuda.synchronize(0)
                pre_alloc = torch.cuda.memory_allocated(0)
                pre_reserved = torch.cuda.memory_reserved(0)
                torch.cuda.reset_peak_memory_stats(0)
                start = time.perf_counter_ns()
                with torch.inference_mode():
                    outputs = self.model.generate(**inputs, generation_config=generation)
                torch.cuda.synchronize(0)
                ms = (time.perf_counter_ns() - start) / 1e6
                peak_alloc = torch.cuda.max_memory_allocated(0)
                peak_reserved = torch.cuda.max_memory_reserved(0)
                generated = outputs[0, input_count:].tolist()
                if any(type(i) is not int or i < 0 for i in generated):
                    raise ValueError
                eos_ids = self.model.generation_config.eos_token_id
                eos_ids = [eos_ids] if isinstance(eos_ids, int) else (eos_ids or [])
                eos = bool(generated and generated[-1] in eos_ids)
                text = self.tokenizer.decode(generated, skip_special_tokens=True)
                count = len(generated)
                value = {'text': text, 'decoded_text_sha256': digest(text.encode('utf-8')),
                         'output_empty': not text.strip(),
                         'generated_token_count': count,
                         'generated_ids_excluding_terminal_eos': count - int(eos),
                         'generated_ids_sha256': digest(canonical(generated)),
                         'termination': 'eos' if eos else ('token_limit' if count == cap else 'other'),
                         'input_token_count': input_count, 'generation_ms': ms,
                         'tokens_per_second': count * 1000 / ms if ms else None,
                         'throughput_unavailable_reason': None if ms else 'zero_duration',
                         'pre_allocated_bytes': pre_alloc, 'pre_reserved_bytes': pre_reserved,
                         'peak_allocated_bytes': peak_alloc, 'peak_reserved_bytes': peak_reserved,
                         'incremental_allocated_bytes': peak_alloc - pre_alloc}
                validate_success(value, cap)
                return value
            except Exception as exc:
                oom = isinstance(exc, torch.cuda.OutOfMemoryError)
                fatal = any(x in str(exc).lower() for x in
                            ('device-side assert', 'illegal memory', 'device lost', 'driver shutting'))
                error = ('OOM' if oom else 'GENERATE', oom, fatal)
        finally:
            inputs = outputs = generated = None
        if error:
            code, oom, fatal = error
            healthy = False if fatal else self._health(oom)
            raise EvaluationError(code, fatal=not healthy, recovered=oom and healthy)
