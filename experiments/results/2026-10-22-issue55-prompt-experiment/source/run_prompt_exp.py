"""prompt-exp-001: 2 models x 2 system prompts, one greedy round per item (not a #37 evaluation result)."""
import gc
import json
import sys
from pathlib import Path

BUNDLE = Path('/abr/coss40/replica-eval-issue37-002/experiments')
sys.path.insert(0, str(BUNDLE / 'evaluation'))
import assets  # noqa: E402
from runtime import Runtime  # noqa: E402

HERE = Path(__file__).resolve().parent
MODELS = {'qwen3-8b': 'Qwen3-8B', 'qwen2.5-7b-instruct': 'Qwen2.5-7B-Instruct'}
PROMPTS = json.loads((HERE / 'prompts.json').read_text(encoding='utf-8'))
probes = json.loads((HERE / 'probes.json').read_text(encoding='utf-8'))['cases']
items = [('probe', c['id'], f"다음은 가상 인물의 기록이야.\n[기록] {c['record']}\n{c['question']}") for c in probes]
for suite in ('human', 'record_qa', 'instructions'):
    items += [(suite, c['id'], c['prompt']) for c in assets.inputs(suite)[1]['cases']]
warmup = assets.inputs('human')[1]['warmup_prompt']

out = HERE / 'generations.jsonl'
assert not out.exists()
with out.open('x', encoding='utf-8') as handle:
    for profile, folder in MODELS.items():
        base_config = assets.inputs('human', profile)[3]
        runtime = Runtime('/abr/coss40/replica-models/' + folder, base_config)
        for prompt_name, system_prompt in PROMPTS.items():
            runtime.config = dict(base_config, system_prompt=system_prompt)
            for _ in range(3):
                runtime.generate(warmup, 160)
            for suite, case_id, text in items:
                row = {'model': profile, 'prompt': prompt_name, 'suite': suite, 'case_id': case_id,
                       'revision': runtime.environment['model_revision']}
                try:
                    result = runtime.generate(text, 160)
                    row.update(status='success', **{k: result[k] for k in (
                        'text', 'termination', 'generated_token_count', 'tokens_per_second', 'generated_ids_sha256')})
                except Exception as exc:
                    row.update(status='failed', error=type(exc).__name__ + ':' + str(getattr(exc, 'code', '')))
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                handle.flush()
        del runtime
        gc.collect()
        import torch
        torch.cuda.empty_cache()
print('PROMPT_EXP_DONE', sum(1 for _ in out.open(encoding='utf-8')))
