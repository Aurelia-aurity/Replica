"""Confirmed synthetic datasets and pinned input contracts (standard library)."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent / 'baseline'
DEFAULT_MODEL_ID = 'Qwen/Qwen2.5-7B-Instruct'
sys.path.append(str(BASE))
from common import EvaluationError, canonical, digest, read_json, write_json


def baseline_module(name):
    spec = importlib.util.spec_from_file_location('issue36_baseline_' + name, BASE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition):
    if not condition:
        raise EvaluationError('CONTRACT')


def profile_settings(name):
    # Pre-registered #37 model settings; the file is the single source of truth.
    profiles = read_json(ROOT / 'model-profiles.json')['profiles']
    require(isinstance(name, str) and name in profiles)
    p = profiles[name]
    require(set(p) == {'model_id', 'revision', 'chat_template_kwargs', 'kobest_add_bos_token'})
    require(isinstance(p['model_id'], str) and isinstance(p['revision'], str) and len(p['revision']) == 40)
    require(isinstance(p['chat_template_kwargs'], dict) and type(p['kobest_add_bos_token']) is bool)
    return p


def validate_release(raw, release):
    data = json.loads(raw)
    require(release == read_json(ROOT / 'release.json'))
    require(data['suite'] in ('human', 'record_qa', 'instructions'))
    require(digest(raw) == release['files'][data['suite'] + '.json'])
    require(data['status'] == 'user_confirmed' and data['synthetic_only'] is True)
    cases = data['cases']
    require(len(cases) == len({c['id'] for c in cases}) == 20)
    require(all(isinstance(c['prompt'], str) and c['prompt'].strip() for c in cases))
    if data['suite'] == 'record_qa':
        for c in cases:
            require(len(c['requested_keys']) == len(set(c['requested_keys'])) > 0)
            require(set(c['requested_keys']) == set(c['gold']))
            require(all(v is None or isinstance(v, str) for v in c['gold'].values()))
    if data['suite'] == 'instructions':
        require(sum(c['kind'] == 'single' for c in cases) == 12)
        require(sum(c['kind'] == 'compound' for c in cases) == 8)
        for c in cases:
            require(len(c['rules']) == 1 if c['kind'] == 'single' else len(c['rules']) > 1)
            for r in c['rules']:
                require(r['type'] in ('contains', 'absent', 'prefix', 'lines'))
                require(type(r['value']) is int and r['value'] > 0 if r['type'] == 'lines'
                        else isinstance(r['value'], str) and bool(r['value']))
    return data


def validate_config(config, dataset):
    profiled = 'model_profile' in config
    profile = profile_settings(config['model_profile']) if profiled else None
    expected = {'system_prompt': '한국어로 간결하고 자연스럽게 답하세요.',
                'do_sample': False, 'warmups': 3, 'rounds': 3,
                'max_new_tokens': 160, 'supplement_max_new_tokens': 320,
                'suite': dataset['suite'], 'model_id': profile['model_id'] if profiled else DEFAULT_MODEL_ID}
    if profiled:
        expected.update(model_revision=profile['revision'], chat_template_kwargs=profile['chat_template_kwargs'])
    else:
        require(not {'model_revision', 'chat_template_kwargs'} & set(config))
    require(all(config.get(k) == v and type(config.get(k)) is type(v) for k, v in expected.items()))
    require(isinstance(config.get('evaluation_code_sha256'), dict) and bool(config['evaluation_code_sha256']))


def inputs(suite, profile=None):
    raw = (ROOT / (suite + '.json')).read_bytes()
    release = read_json(ROOT / 'release.json')
    for filename, hash_value in release['files'].items():
        require(digest((ROOT / filename).read_bytes()) == hash_value)
    data = validate_release(raw, release)
    # Reuse the baseline's immutable runner and strict journal reader. Its phase
    # name "baseline" means the scheduled main attempts for this suite too.
    release = dict(release, rubric_sha256=digest(canonical(data['category_rubrics'])))
    config = {'model_id': DEFAULT_MODEL_ID, 'suite': suite,
              'system_prompt': '한국어로 간결하고 자연스럽게 답하세요.',
              'do_sample': False, 'warmups': 3, 'rounds': 3,
              'max_new_tokens': 160, 'supplement_max_new_tokens': 320,
              'evaluation_code_sha256': {str(p.relative_to(ROOT.parent)): digest(p.read_bytes())
                for directory in (ROOT, BASE) for p in sorted(directory.glob('*.py'))}}
    if profile is not None:
        p = profile_settings(profile)
        config.update(model_id=p['model_id'], model_profile=profile, model_revision=p['revision'],
                      chat_template_kwargs=p['chat_template_kwargs'])
    validate_config(config, data)
    return raw, data, release, config


def snapshot_release(raw, release):
    # The runner adds this derived field for the baseline reader's common shape.
    base = dict(release)
    value = base.pop('rubric_sha256')
    data = validate_release(raw, base)
    require(value == digest(canonical(data['category_rubrics'])))
    return data


def load_run(path):
    return baseline_module('report').load_run(path, snapshot_release, validate_config)
