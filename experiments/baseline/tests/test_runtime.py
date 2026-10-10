"""Exercise real adapter with fake CUDA/tokenizer/model boundaries."""
import sys
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Runtime
from common import EvaluationError, inputs

class OOM(Exception):
    pass

class AdapterTests(unittest.TestCase):
    def runtime(self, ids=None, text='답변', failure=None):
        log = []
        ids = [5, 2] if ids is None else ids
        class CUDA:
            OutOfMemoryError = OOM
            def synchronize(self, device): log.append('sync')
            def memory_allocated(self, device): log.append('allocated'); return 100
            def memory_reserved(self, device): log.append('reserved'); return 200
            def reset_peak_memory_stats(self, device): log.append('reset')
            def max_memory_allocated(self, device): log.append('peak_alloc'); return 150
            def max_memory_reserved(self, device): log.append('peak_reserved'); return 200
            def empty_cache(self): log.append('empty_cache')
        class Batch(dict):
            def to(self, device): log.append('transfer'); return self
        class Tokenizer:
            eos_token_id = 2
            def apply_chat_template(self, messages, **kwargs):
                log.append('template')
                self.messages, self.options = messages, kwargs
                return Batch(input_ids=types.SimpleNamespace(shape=(1, 10)))
            def decode(self, generated, **kwargs):
                log.append('decode'); return text
        class Output:
            def __getitem__(self, key):
                assert key == (0, slice(10, None))
                return self
            def tolist(self): return ids
        class Model:
            config = types.SimpleNamespace(max_position_embeddings=4096)
            generation_config = types.SimpleNamespace(eos_token_id=[2, 3])
            def generate(self, **kwargs):
                log.append('generate')
                self.kwargs = kwargs
                if failure: raise failure
                return Output()
        adapter = Runtime.__new__(Runtime)
        adapter.torch = types.SimpleNamespace(cuda=CUDA(), inference_mode=nullcontext,
                                             empty=lambda *a, **kw: log.append('probe'))
        adapter.GenerationConfig = lambda **kw: types.SimpleNamespace(**kw)
        adapter.config = inputs()[3]
        adapter.tokenizer, adapter.model = Tokenizer(), Model()
        return adapter, log
    def test_boundaries_and_baseline_template_policy(self):
        adapter, log = self.runtime()
        def clock(): log.append('clock'); return 100 if log.count('clock') == 1 else 10000100
        with patch('runtime.time.perf_counter_ns', side_effect=clock):
            result = adapter.generate('질문', 160)
        self.assertEqual(log, ['template', 'transfer', 'sync', 'allocated', 'reserved', 'reset',
                               'clock', 'generate', 'sync', 'clock', 'peak_alloc', 'peak_reserved', 'decode'])
        self.assertEqual(adapter.tokenizer.messages, [
            {'role': 'system', 'content': '한국어로 간결하고 자연스럽게 답하세요.'},
            {'role': 'user', 'content': '질문'}])
        self.assertEqual(adapter.tokenizer.options, dict(tokenize=True, add_generation_prompt=True,
                                                        return_dict=True, return_tensors='pt'))
        cfg = adapter.model.kwargs['generation_config']
        self.assertFalse(cfg.do_sample)
        self.assertEqual((cfg.max_new_tokens, cfg.eos_token_id, cfg.pad_token_id), (160, [2, 3], 2))
        self.assertEqual(result['generation_ms'], 10)
        adapter.generate('다른 질문', 160)
        self.assertEqual(adapter.tokenizer.messages[-1]['content'], '다른 질문')
        self.assertEqual(len(adapter.tokenizer.messages), 2)
        self.assertNotIn('empty_cache', log)
    def test_empty_whitespace_eos_and_limit(self):
        for ids, text, termination in (([2], '', 'eos'), ([3], '  ', 'eos'),
                                       ([1] * 159 + [2], '답', 'eos'),
                                       ([1] * 160, '답', 'token_limit')):
            adapter, _ = self.runtime(ids, text)
            result = adapter.generate('질문', 160)
            self.assertEqual(result['termination'], termination)
            self.assertEqual(result['output_empty'], not text.strip())
    def test_oom_health_and_fatal(self):
        adapter, log = self.runtime(failure=OOM('private-path'))
        with self.assertRaises(EvaluationError) as captured:
            adapter.generate('질문', 160)
        self.assertTrue(captured.exception.recovered)
        self.assertFalse(captured.exception.fatal)
        self.assertEqual(log.count('generate'), 1)
        self.assertIn('empty_cache', log)
        adapter, log = self.runtime(failure=RuntimeError('device-side assert'))
        with self.assertRaises(EvaluationError) as captured:
            adapter.generate('질문', 160)
        self.assertTrue(captured.exception.fatal)
        self.assertNotIn('probe', log)
    def test_health_failure_and_input_context(self):
        adapter, log = self.runtime(failure=RuntimeError('failure'))
        adapter._health = lambda oom: False
        with self.assertRaises(EvaluationError) as captured:
            adapter.generate('질문', 160)
        self.assertTrue(captured.exception.fatal)
        adapter, log = self.runtime()
        adapter.model.config.max_position_embeddings = 100
        with self.assertRaises(EvaluationError) as captured:
            adapter.generate('질문', 160)
        self.assertEqual(captured.exception.code, 'INPUT')
        self.assertNotIn('generate', log)

if __name__ == '__main__':
    unittest.main()
