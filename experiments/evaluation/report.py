"""Validate original logs and write distinct machine/human/operational reports."""
import argparse
import json
from pathlib import Path
from assets import EvaluationError, baseline_module, digest, load_run, read_json, require, write_json
from scoring import aggregate, instructions, mean, qa
import human


def report(run, labels=None):
    suite = run['dataset']['suite']
    result = {'schema_version': 1, 'suite': suite, 'execution_kind': run['metadata']['execution_kind'],
              'run_id': run['metadata']['run_id'], 'dataset_sha256': run['metadata']['dataset_sha256'],
              'config': run['metadata']['config'],
              'raw_artifact_sha256': run['raw_artifact_sha256'], 'counts': run['counts'],
              'schedule_complete': run['schedule_complete'], 'journal_damaged': run['journal_damaged']}
    main = [e['result'] for e in run['results'].values() if e['phase'] == 'baseline' and e['status'] == 'success']
    result['operational'] = {'scope': 'successful_main_attempts_only_including_empty',
        'successful_attempts': len(main), 'mean_generation_ms': mean([v['generation_ms'] for v in main]),
        'mean_tokens_per_second': mean([v['tokens_per_second'] for v in main]),
        'input_tokens': sum(v['input_token_count'] for v in main),
        'generated_tokens_including_terminal_eos': sum(v['generated_token_count'] for v in main),
        'max_peak_allocated_bytes': max((v['peak_allocated_bytes'] for v in main), default=None),
        'max_peak_reserved_bytes': max((v['peak_reserved_bytes'] for v in main), default=None),
        'load': run['load'], 'cache_recovery_occurred': run['cache_recovery_occurred'],
        'not_measured': ['TTFT', 'end_to_end_latency', 'whole_gpu_VRAM']}
    if suite in ('human', 'retrospective_grounding'):
        result['evaluation_axis'] = 'human_semantic_judgment'
        result['human'] = human.aggregate(run, labels or human.template(run))
        return result
    result['evaluation_axis'] = 'deterministic_mechanical'
    rounds = []
    scorer = qa if suite == 'record_qa' else instructions
    for r in range(1, 4):
        rows, pending = [], []
        for c in run['dataset']['cases']:
            aid = f'r{r}-{c["id"]}'
            e = run['results'].get(aid)
            if e is None:
                pending.append(aid)
                continue
            value = scorer(c, e['result']['text'] if e['status'] == 'success' else '', e['status'])
            rows.append(dict(value, case_id=c['id'], attempt_id=aid))
        rounds.append({'round': r, 'planned': 20, 'attempted': len(rows), 'pending': pending,
                       'complete': not pending, 'scores': aggregate(rows, suite), 'rows': rows})
    result['rounds'] = rounds
    complete = all(r['complete'] for r in rounds)
    # Preserve subgroup denominator diagnostics per round; average scores only
    # after all scheduled main outcomes exist, including attempted failures.
    result['three_round_mean'] = None
    if complete:
        scores = [r['scores'] for r in rounds]
        if suite == 'record_qa':
            result['three_round_mean'] = {k: mean([s[k] for s in scores]) for k in ('whole_accuracy', 'format_validity')}
            result['three_round_mean'].update({k: mean([s[k]['score'] for s in scores]) for k in ('slots', 'known', 'null')})
        else:
            result['three_round_mean'] = {k: mean([s[k] for s in scores]) for k in scores[0]}
    result['main_complete'] = complete
    return result


def report_with_label_file(run, path=None):
    raw = Path(path).read_bytes() if path else None
    value = report(run, json.loads(raw) if raw is not None else None)
    if raw is not None:
        value['labels_sha256'] = digest(raw)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--labels', type=Path)
    parser.add_argument('--retrospective-grounding', action='store_true', help='Original baseline G01–G05; original scores preserved')
    args = parser.parse_args()
    try:
        run = human.retrospective(baseline_module('report').load_run(args.run)) if args.retrospective_grounding else load_run(args.run)
        require(not args.labels or run['dataset']['suite'] in ('human', 'retrospective_grounding'))
        value = report_with_label_file(run, args.labels)
        require(not args.output.resolve().is_relative_to(args.run.resolve()))
        args.output.mkdir(parents=True, exist_ok=False)
        write_json(args.output / 'report.json', value)
        if run['dataset']['suite'] in ('human', 'retrospective_grounding') and args.labels is None:
            write_json(args.output / 'human-labels.json', human.template(run))
        print('검증·집계 완료. 미실행·미채점 여부는 report.json에서 확인하세요.')
        return 0
    except (EvaluationError, ValueError, OSError, KeyError, TypeError):
        print('검증 실패: 입력 hash·실행 로그·채점 양식·새 출력 경로를 확인하세요.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
