"""Standard-library artifact contracts; no GPU dependency."""
import hashlib
import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
CATEGORIES = ('naturalness', 'instructions', 'usefulness', 'grounding')
MODEL_ID = 'Qwen/Qwen2.5-7B-Instruct'
REFERENCE_COMMIT = '705f2c2c9f9c0f426ac3376f9b9132021fb67139'

class EvaluationError(Exception):
    def __init__(self, code, fatal=False, recovered=False):
        self.code, self.fatal, self.recovered = code, fatal, recovered
        super().__init__(code)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def now():
    return datetime.now(timezone.utc).isoformat()

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())

def validate_release(dataset_bytes, release):
    dataset = json.loads(dataset_bytes)
    if (dataset['status'] != 'user_confirmed' or
        release['confirmation_status'] != 'user_confirmed' or
        release['dataset_id'] != dataset['dataset_id'] or
        release['version'] != dataset['version'] or
        digest(dataset_bytes) != release['dataset_sha256'] or
        digest(canonical(dataset['category_rubrics'])) != release['rubric_sha256']):
        raise EvaluationError('RELEASE')
    cases = dataset['cases']
    if (len(cases) != 20 or len({c['id'] for c in cases}) != 20 or
        not dataset['synthetic_only'] or
        tuple(dataset['category_rubrics']) != CATEGORIES or
        any(sum(c['category'] == cat for c in cases) != 5 for cat in CATEGORIES) or
        any(not isinstance(c['prompt'], str) or not c['prompt'].strip() or
            len(c['prompt']) > 16000 for c in cases) or
        [c['evidence_mode'] for c in cases if c['category'] == 'grounding'] !=
            ['absent'] * 3 + ['provided'] * 2):
        raise EvaluationError('DATASET')
    return dataset

def inputs():
    raw = (ROOT / 'cases.json').read_bytes()
    release, config = read_json(ROOT / 'dataset-release.json'), read_json(ROOT / 'config.json')
    dataset = validate_release(raw, release)
    validate_config(config, dataset)
    return raw, dataset, release, config

def validate_config(config, dataset):
    if (config['model_id'] != MODEL_ID or config['baseline_reference_commit'] != REFERENCE_COMMIT or
        config['system_prompt'] != dataset['generation']['system_prompt'] or
        config['do_sample'] is not False or config['warmups'] != 3 or
        config['rounds'] != 3 or config['max_new_tokens'] != 160 or
        config['supplement_max_new_tokens'] != 320):
        raise EvaluationError('CONFIG')

def slots(dataset, config):
    warm = [{'attempt_id': f'w{i}', 'phase': 'warmup', 'case_id': None,
             'round': None, 'cap': 160, 'parent_id': None,
             'prompt': dataset['warmup_prompt']} for i in range(1, 4)]
    base = [{'attempt_id': f'r{r}-{c["id"]}', 'phase': 'baseline',
             'case_id': c['id'], 'round': r, 'cap': 160, 'parent_id': None,
             'prompt': c['prompt']} for r in range(1, 4) for c in dataset['cases']]
    return warm, base

def supplemental(slot):
    return dict(slot, attempt_id='s-' + slot['attempt_id'], phase='supplement',
                cap=320, parent_id=slot['attempt_id'])

def public_slot(slot):
    return {k: v for k, v in slot.items() if k != 'prompt'}

def validate_success(value, cap):
    count = value['generated_token_count']
    eos = value['termination'] == 'eos'
    if (type(count) is not int or not 0 <= count <= cap or
        value['termination'] not in ('eos', 'token_limit', 'other') or (eos and count == 0) or
        (value['termination'] == 'token_limit' and count != cap) or
        (value['termination'] == 'other' and count >= cap) or
        value['generated_ids_excluding_terminal_eos'] != count - int(eos) or
        not isinstance(value['text'], str) or
        digest(value['text'].encode('utf-8')) != value['decoded_text_sha256'] or
        value['output_empty'] != (not value['text'].strip()) or
        not isinstance(value['generated_ids_sha256'], str) or
        len(value['generated_ids_sha256']) != 64 or
        any(c not in '0123456789abcdef' for c in value['generated_ids_sha256'])):
        raise EvaluationError('RESULT')
    ms = value['generation_ms']
    if not isinstance(ms, (float, int)) or isinstance(ms, bool) or not math.isfinite(ms) or ms < 0:
        raise EvaluationError('METRIC')
    for k in ('pre_allocated_bytes', 'pre_reserved_bytes', 'peak_allocated_bytes',
              'peak_reserved_bytes', 'incremental_allocated_bytes', 'input_token_count'):
        if type(value[k]) is not int or value[k] < 0:
            raise EvaluationError('METRIC')
    if (value['peak_allocated_bytes'] < value['pre_allocated_bytes'] or
        value['peak_reserved_bytes'] < value['pre_reserved_bytes'] or
        value['incremental_allocated_bytes'] != value['peak_allocated_bytes'] - value['pre_allocated_bytes']):
        raise EvaluationError('METRIC')
    expected = count * 1000 / ms if ms else None
    if value['tokens_per_second'] != expected or value['throughput_unavailable_reason'] != (None if ms else 'zero_duration'):
        raise EvaluationError('METRIC')
