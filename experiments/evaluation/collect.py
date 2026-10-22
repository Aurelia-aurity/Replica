"""Revalidate runs and collect separate evaluation axes, without a composite score."""
import argparse
from pathlib import Path
from assets import ROOT, EvaluationError, baseline_module, digest, load_run, read_json, require, write_json
from report import report
import json
import human
from kobest import hash_file, profile_identity, validate_result


def collect(paths, labels=None, kobest_run=None, baseline_run=None):
    results = {}
    fingerprints = []
    profiles = []
    execution_kinds = []
    for suite in ('human', 'record_qa', 'instructions'):
        run = load_run(paths[suite])
        require(run['dataset']['suite'] == suite)
        fingerprints.append(run['metadata']['config']['model_artifacts_sha256'])
        profiles.append(run['metadata']['config'].get('model_profile'))
        execution_kinds.append(run['metadata']['execution_kind'])
        results[suite] = report(run, labels if suite == 'human' else None)
    require(all(f == fingerprints[0] for f in fingerprints) and len(set(execution_kinds)) == 1)
    require(len(set(profiles)) == 1)
    benchmark = {'state': 'pending', 'report': None}
    if kobest_run:
        path = Path(kobest_run)
        summary = read_json(path / 'report.json')
        identity = read_json(path / 'identity.json')
        require(identity['lock'] == read_json(ROOT / 'kobest-lock.json'))
        require(identity['model_sha256'] == fingerprints[0])
        require(identity['smoke'] is False and identity['apply_chat_template'] is False and identity['num_fewshot'] == 0)
        _, extra = profile_identity(profiles[0])
        require({k: identity.get(k) for k in ('model_profile', 'model_id', 'model_revision', 'add_bos_token')} ==
                {k: extra.get(k) for k in ('model_profile', 'model_id', 'model_revision', 'add_bos_token')})
        require(summary['identity_sha256'] == hash_file(path / 'identity.json'))
        name = summary['result_file']
        require(Path(name).name == name and name.startswith('result-') and name.endswith('.json'))
        require(summary['result_sha256'] == hash_file(path / name))
        result = validate_result(read_json(path / name), identity['lock'])
        for k, v in result.items():
            require(summary[k] == v)
        benchmark = {'state': 'complete_full_test', 'report': summary}
    historical = {'state': 'not_supplied', 'report': None}
    if baseline_run:
        original = baseline_module('report').load_run(baseline_run)
        historical = {'state': 'retrospective_labels_pending', 'report': report(human.retrospective(original)),
                      'original_scores_preserved': True, 'already_known_outputs_not_blind': True}
    return {'schema_version': 1, 'execution_kind': execution_kinds[0], 'model_artifacts_sha256': fingerprints[0],
            'human_semantic_quality': results['human'],
            'deterministic_mechanical': {s: results[s] for s in ('record_qa', 'instructions')},
            'kobest': benchmark, 'retrospective_baseline': historical,
            'overall_composite_score': None,
            'evaluation_data_complete': execution_kinds[0] == 'school_gpu_user_run' and
                 results['human']['schedule_complete'] and results['human']['human']['complete'] and
                 all(results[s]['schedule_complete'] for s in ('record_qa', 'instructions')) and
                 benchmark['state'] == 'complete_full_test',
            'interpretation': 'Separate axes; no service readiness claim. Shared materials and repeated outputs are not independent cases.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('human-run', 'qa-run', 'instructions-run', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--human-labels', type=Path)
    parser.add_argument('--kobest-run', type=Path)
    parser.add_argument('--baseline-run', type=Path)
    args = parser.parse_args()
    try:
        label_bytes = args.human_labels.read_bytes() if args.human_labels else None
        result = collect({'human': args.human_run, 'record_qa': args.qa_run, 'instructions': args.instructions_run},
                          json.loads(label_bytes) if label_bytes is not None else None, args.kobest_run, args.baseline_run)
        if label_bytes is not None:
            result['human_semantic_quality']['labels_sha256'] = digest(label_bytes)
        require(all(not args.output.resolve().is_relative_to(p.resolve()) for p in
                    [args.human_run, args.qa_run, args.instructions_run] +
                    ([args.kobest_run] if args.kobest_run else []) + ([args.baseline_run] if args.baseline_run else [])))
        args.output.mkdir(parents=True, exist_ok=False)
        write_json(args.output / 'report.json', result)
        return 0
    except (EvaluationError, KeyError, TypeError, ValueError, OSError):
        print('통합 실패: 과제·모델 hash·원본 로그·KoBEST 완료 여부를 확인하세요.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
