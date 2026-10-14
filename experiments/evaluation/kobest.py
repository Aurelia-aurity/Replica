"""Pinned official KoBEST prompts and log-likelihood evaluation, with resumable cache."""
import argparse
import importlib.metadata
import json
import math
import os
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path
from assets import ROOT, EvaluationError, canonical, digest, read_json, require, write_json
from common import now


def prepare(data_dir):
    lock = read_json(ROOT / 'kobest-lock.json')
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=False)
    for name, expected in lock['test_files'].items():
        url = f'https://huggingface.co/datasets/{lock["dataset_id"]}/resolve/{lock["dataset_revision"]}/{name}/test.jsonl'
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read()
        require(digest(raw) == expected['sha256'])
        require(len([json.loads(line) for line in raw.splitlines() if line.strip()]) == expected['test_count'])
        (data_dir / (name + '.jsonl')).write_bytes(raw)
    write_json(data_dir / 'data-lock.json', lock)


def hash_file(path):
    import hashlib
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def package_inventory(distributions=None):
    # Keep every visible distribution, including shadowed system versions.
    # A venv overlay must never be hidden by a last-write-wins dictionary.
    rows = []
    visible = distributions if distributions is not None else importlib.metadata.distributions()
    for priority, dist in enumerate(visible):
        name = dist.metadata.get('Name')
        if name:
            rows.append({'name': name.lower().replace('_', '-').replace('.', '-'),
                         'version': dist.version,
                         'priority': priority,
                         'location_sha256': digest(str(Path(dist.locate_file('')).resolve()).encode())})
    return rows


def verify_source(harness, data_dir):
    lock = read_json(ROOT / 'kobest-lock.json')
    revision = subprocess.check_output(['git', '-C', str(harness), 'rev-parse', 'HEAD'], text=True).strip()
    status = subprocess.check_output(['git', '-C', str(harness), 'status', '--porcelain'], text=True).strip()
    require(revision == lock['harness_revision'] and not status)
    require(read_json(data_dir / 'data-lock.json') == lock)
    for name, info in lock['test_files'].items():
        require(hash_file(data_dir / (name + '.jsonl')) == info['sha256'])
    for name, sha in lock['harness_task_sha256'].items():
        require(hash_file(harness / 'lm_eval/tasks/kobest' / name) == sha)
    return lock


def tasks(harness, data_dir, lock):
    from lm_eval.api.task import ConfigurableTask
    from lm_eval.tasks._yaml_loader import load_yaml
    result = []
    for name in lock['tasks']:
        cfg = load_yaml(harness / 'lm_eval/tasks/kobest' / (name + '.yaml'), resolve_func=True)
        # F1 uses an extra remote evaluate module in the upstream configuration.
        # Retain native accuracy and HellaSwag acc_norm; optional F1 is omitted
        # rather than downloading executable metric code in the GPU session.
        cfg['metric_list'] = [m for m in cfg['metric_list'] if m['metric'] != 'f1']
        # Only the dataset transport changes: bytes are pinned copies of the
        # official test JSONL. Official preprocessing, prompts, choices, labels
        # and metrics stay intact. No train/validation examples are used.
        cfg.update(dataset_path='json', dataset_name=None,
                   dataset_kwargs={'data_files': {'test': str(data_dir / (name.removeprefix('kobest_') + '.jsonl'))}},
                   training_split=None, validation_split=None, num_fewshot=0)
        task = ConfigurableTask(config=cfg)
        require(len(task.test_docs()) == lock['test_files'][name.removeprefix('kobest_')]['test_count'])
        require(task.OUTPUT_TYPE == 'multiple_choice')
        result.append(task)
    return result


def validate_result(result, lock, smoke=False):
    require(result is not None)
    metrics = {}
    for name in lock['tasks']:
        expected = 2 if smoke else lock['test_files'][name.removeprefix('kobest_')]['test_count']
        samples = result['samples'][name]
        require(len(samples) == expected and len({r['doc_id'] for r in samples}) == expected)
        require(set(r['doc_id'] for r in samples) == set(range(expected)))
        choice_count = 4 if name == 'kobest_hellaswag' else 2
        require(all(len(r['resps']) == choice_count and
                    all(len(resp) == 1 and len(resp[0]) == 2 and
                        type(resp[0][0]) in (float, int) and math.isfinite(resp[0][0]) and
                        type(resp[0][1]) is bool for resp in r['resps']) for r in samples))
        def metric(key):
            require(all(type(r.get(key)) in (bool, int, float) and r[key] in (0, 1) for r in samples))
            value = result['results'][name][key + ',none']
            require(type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1)
            require(abs(value - sum(r[key] for r in samples) / expected) < 1e-12)
            return value
        acc = metric('acc')
        if name == 'kobest_hellaswag':
            metric('acc_norm')
        metrics[name] = {'accuracy': acc, 'computed': expected, 'expected': expected,
                         'additional_metrics': result['results'][name]}
    return {'complete_full_test': not smoke, 'execution_kind': 'school_gpu_smoke' if smoke else 'school_gpu_full_test',
            'tasks': metrics, 'project_equal_task_mean': None if smoke else sum(v['accuracy'] for v in metrics.values()) / 5,
            'aggregate_is_official': False}


def execute(model_path, harness, data_dir, output, resume=False, smoke=False):
    harness, data_dir, model_path, output = map(lambda p: Path(p).resolve(), (harness, data_dir, model_path, output))
    lock = verify_source(harness, data_dir)
    require(sys.version_info[:2] == (3, 12) and sys.prefix != sys.base_prefix and bool(os.environ.get('SLURM_JOB_ID')))
    require(model_path.is_dir() and (model_path / 'config.json').is_file())
    # Fingerprint every model artifact (not just its model name) and all evaluator
    # sources/config/packages. Resuming cannot combine differing calculations.
    model_hashes = {p.name: hash_file(p) for p in sorted(model_path.iterdir()) if p.is_file() and
                    (p.name.endswith('.safetensors') or p.name in
                     ('config.json', 'generation_config.json', 'tokenizer.json', 'tokenizer_config.json',
                      'special_tokens_map.json', 'model.safetensors.index.json', 'vocab.json', 'merges.txt'))}
    require(any(k.endswith('.safetensors') for k in model_hashes))
    packages = package_inventory()
    identity = {'lock': lock, 'model_sha256': model_hashes, 'packages': packages,
                'python': sys.version, 'smoke': smoke,
                'wrapper_sha256': digest(Path(__file__).read_bytes()),
                'evaluator_code_sha256': {p.name: digest(p.read_bytes()) for p in sorted(ROOT.glob('*.py'))},
                'batch_size': 1, 'dtype': 'bfloat16', 'num_fewshot': 0,
                'apply_chat_template': False, 'system_instruction': None}
    if resume:
        require(read_json(output / 'identity.json') == identity and not (output / 'report.json').exists())
    else:
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / 'identity.json', identity)
    attempt = str(uuid.uuid4())
    def event(kind, **values):
        with (output / 'events.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(canonical(dict(type=kind, attempt_id=attempt, time=now(), **values)).decode() + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    event('attempt_start', resume=resume, smoke=smoke)
    try:
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['HF_DATASETS_OFFLINE'] = '1'
        os.environ['HF_EVALUATE_OFFLINE'] = '1'
        sys.path.insert(0, str(harness))
        import lm_eval
        require(Path(lm_eval.__file__).resolve().is_relative_to(harness))
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from lm_eval.models.huggingface import HFLM
        from lm_eval.evaluator import simple_evaluate
        require(torch.cuda.is_available() and torch.cuda.is_bf16_supported())
        ts = tasks(harness, data_dir, lock)
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True, trust_remote_code=False,
                     use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='eager')
        lm = HFLM(pretrained=model, tokenizer=tokenizer, device='cuda:0', batch_size=1,
                  dtype='bfloat16', add_bos_token=False)
        # The official cache records individual likelihood requests. It can
        # survive an OOM/interruption, but cached requests alone are never scores.
        result = simple_evaluate(model=lm, tasks=ts, num_fewshot=0, batch_size=1,
                    use_cache=str(output / 'likelihood-cache'), cache_requests=False,
                    apply_chat_template=False, system_instruction=None,
                    limit=2 if smoke else None, bootstrap_iters=0, log_samples=True)
        summary = validate_result(result, lock, smoke)
        # Harness metrics can include numpy scalar/tuple data; serialization is
        # delegated to its own explicit non-JSON type handler.
        from lm_eval.utils import handle_non_serializable
        serial = json.loads(json.dumps(result, default=handle_non_serializable))
        write_json(output / ('result-' + attempt + '.json'), serial)
        summary['result_file'] = 'result-' + attempt + '.json'
        summary['result_sha256'] = hash_file(output / summary['result_file'])
        summary['identity_sha256'] = hash_file(output / 'identity.json')
        write_json(output / 'report.json', summary)
        event('attempt_end', status='success', complete_full_test=not smoke)
        return 0
    except (Exception, KeyboardInterrupt):
        event('attempt_end', status='calculation_failed', final_scores_available=False)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--data-dir', type=Path, required=True)
    run = sub.add_parser('run')
    for name in ('model-path', 'harness', 'data-dir', 'output'):
        run.add_argument('--' + name, type=Path, required=True)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            prepare(args.data_dir)
            return 0
        return execute(args.model_path, args.harness, args.data_dir, args.output, args.resume, args.smoke)
    except (EvaluationError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('KoBEST 준비·실행 실패. revision·환경·새 출력 경로를 확인하세요.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
