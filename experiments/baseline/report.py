"""Validate immutable raw artifacts and create a new scoring/report directory."""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from common import (CATEGORIES, EvaluationError, canonical, digest, now, public_slot,
                    read_json, slots, supplemental, validate_config, validate_release, validate_success, write_json)

def require(condition):
    if not condition:
        raise EvaluationError('JOURNAL')

def load_run(path):
    path = Path(path)
    snapshot = {name: (path / name).read_bytes() for name in ('metadata.json', 'cases.json', 'events.jsonl')}
    meta = json.loads(snapshot['metadata.json'])
    raw = snapshot['cases.json']
    dataset = validate_release(raw, meta['release'])
    require(meta['dataset_sha256'] == digest(raw))
    require(meta['rubric_sha256'] == meta['release']['rubric_sha256'])
    require(meta['config_sha256'] == digest(canonical(meta['config'])))
    validate_config(meta['config'], dataset)
    require(meta['execution_kind'] in ('mock', 'school_gpu_user_run'))
    data = snapshot['events.jsonl']
    damage = bool(data and not data.endswith(b'\n'))
    lines = data.splitlines(keepends=True)
    if damage:
        lines.pop()  # only an incomplete trailing line may be ignored
    events = [json.loads(line) for line in lines]
    warm, baseline = slots(dataset, meta['config'])
    schedule = warm + baseline
    supplemental_slots = []
    starts, results, active = {}, {}, None
    cursor, load_state, ended, recovered = 0, 'none', False, False
    terminal = load = None
    for index, event in enumerate(events):
        require(event['seq'] == index and event['run_id'] == meta['run_id'] and not ended)
        kind = event['type']
        if kind == 'run_start':
            require(index == 0)
        elif kind == 'load_start':
            require(index == 1 and load_state == 'none')
            load_state = 'started'
        elif kind == 'load_result':
            require(load_state == 'started' and index == 2)
            load_state = event['status']
            require(load_state in ('success', 'failed'))
            load = event
            if load_state == 'success':
                require(type(event['load_ms']) in (float, int)
                        and math.isfinite(event['load_ms']) and event['load_ms'] >= 0)
        elif kind == 'attempt_start':
            require(load_state == 'success' and active is None)
            if cursor == len(schedule):
                schedule.extend(supplemental_slots)
                supplemental_slots = []
            require(cursor < len(schedule))
            expected = public_slot(schedule[cursor])
            require(all(event.get(k) == v for k, v in expected.items()))
            require(event['after_recovery'] is recovered)
            aid = event['attempt_id']
            require(aid not in starts)
            starts[aid], active = event, aid
            cursor += 1
        elif kind == 'attempt_result':
            aid = event['attempt_id']
            require(active == aid and aid not in results)
            require(all(event.get(k) == starts[aid].get(k) for k in
                        ('phase', 'case_id', 'round', 'cap', 'parent_id', 'after_recovery')))
            require(event['status'] in ('success', 'failed'))
            if event['status'] == 'success':
                validate_success(event['result'], event['cap'])
                if event['phase'] == 'baseline' and event['result']['termination'] == 'token_limit':
                    supplemental_slots.append(supplemental(schedule[cursor - 1]))
            else:
                require(type(event['fatal']) is bool and type(event['recovery_changed_cache']) is bool)
                require(event['error_code'] in ('INPUT', 'OOM', 'GENERATE', 'RUNTIME', 'RESULT', 'METRIC'))
                require(not event['recovery_changed_cache'] or (event['error_code'] == 'OOM' and not event['fatal']))
                recovered = recovered or event['recovery_changed_cache']
            results[aid], active = event, None
        elif kind == 'run_end':
            require(index >= 2)
            terminal, ended = event, True
        else:
            raise EvaluationError('JOURNAL')
        # A failed warmup or fatal result must end the run, never advance it.
        if kind == 'attempt_start' and index:
            prior = events[index - 1]
            require(not (prior['type'] == 'attempt_result' and prior['status'] == 'failed'
                         and (prior['phase'] == 'warmup' or prior['fatal'])))
    expected_supplements = [supplemental(s) for s in baseline
                            if s['attempt_id'] in results
                            and results[s['attempt_id']]['status'] == 'success'
                            and results[s['attempt_id']]['result']['termination'] == 'token_limit']
    all_schedule = warm + baseline + expected_supplements
    counts = {}
    for phase in ('warmup', 'baseline', 'supplement'):
        ids = [s['attempt_id'] for s in all_schedule if s['phase'] == phase]
        succeeded = [i for i in ids if i in results and results[i]['status'] == 'success']
        failed = [i for i in ids if i in results and results[i]['status'] == 'failed']
        interrupted = [i for i in ids if i in starts and i not in results]
        unexecuted = [i for i in ids if i not in starts]
        counts[phase] = {'expected': len(ids), 'started': len(ids) - len(unexecuted),
                         'success': succeeded, 'failed': failed,
                         'interrupted': interrupted, 'unexecuted': unexecuted}
    baseline_complete = not counts['baseline']['unexecuted'] and not counts['baseline']['interrupted']
    supplements_complete = baseline_complete and not counts['supplement']['unexecuted'] and not counts['supplement']['interrupted']
    warm_ok = len(counts['warmup']['success']) == 3
    schedule_complete = warm_ok and baseline_complete and supplements_complete
    failure_count = sum(len(c['failed']) for c in counts.values())
    if terminal:
        require(terminal['failures'] == failure_count)
        require(terminal['baseline_complete'] == baseline_complete)
        require(terminal['supplements_complete'] == supplements_complete)
        require(terminal['schedule_complete'] == schedule_complete)
        require(terminal['status'] in ('success', 'failed', 'aborted', 'interrupted'))
        require(terminal['status'] != 'success' or (schedule_complete and not failure_count))
    return {'metadata': meta, 'dataset': dataset, 'results': results, 'counts': counts,
            'load': load, 'journal_damaged': damage, 'cache_recovery_occurred': recovered,
            'baseline_complete': baseline_complete, 'supplements_complete': supplements_complete,
            'schedule_complete': bool(terminal and schedule_complete and not damage),
            'run_success': bool(terminal and terminal['status'] == 'success' and not damage),
            'raw_artifact_sha256': {p: digest(raw) for p, raw in snapshot.items()}}

def score_template(run):
    meta = run['metadata']
    rows = []
    for case in run['dataset']['cases']:
        aid = 'r1-' + case['id']
        result = run['results'].get(aid)
        eligible = bool(result and result['status'] == 'success')
        value = result['result'] if eligible else None
        rows.append({'case_id': case['id'], 'attempt_id': aid, 'category': case['category'],
                     'eligible': eligible, 'decoded_text_sha256': value['decoded_text_sha256'] if value else None,
                     'score': None, 'reason': None, 'fabricated_personal_fact': None})
    return {'schema_version': 1, 'run_id': meta['run_id'],
            'dataset_sha256': meta['dataset_sha256'], 'rubric_sha256': meta['rubric_sha256'],
            'scoring_method': 'human_manual', 'graded_at': None, 'rows': rows}

def validate_scores(run, scores):
    template = score_template(run)
    for key in ('schema_version', 'run_id', 'dataset_sha256', 'rubric_sha256', 'scoring_method'):
        require(scores[key] == template[key])
    require(len(scores['rows']) == 20)
    rows = {}
    for row in scores['rows']:
        require(row['case_id'] not in rows)
        rows[row['case_id']] = row
    for expected in template['rows']:
        row = rows[expected['case_id']]
        for key in ('attempt_id', 'category', 'eligible', 'decoded_text_sha256'):
            require(row[key] == expected[key])
        if row['score'] is None:
            require(row['reason'] is None and row['fabricated_personal_fact'] is None)
        else:
            require(row['eligible'] and type(row['score']) is int and 1 <= row['score'] <= 5
                    and isinstance(row['reason'], str) and bool(row['reason'].strip())
                    and type(row['fabricated_personal_fact']) is bool)
    if any(r['score'] is not None for r in rows.values()):
        from datetime import datetime
        require(isinstance(scores['graded_at'], str))
        require(datetime.fromisoformat(scores['graded_at']).tzinfo is not None)
    return list(rows.values())

def stats(values):
    values = [v for v in values if v is not None]
    return {'n': len(values), 'median': statistics.median(values) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}

def literal_checks(case_id, text):
    """Literal evidence only; sentence/register/question judgments remain human."""
    lines = text.splitlines()
    if case_id == 'I02':
        return {'exactly_two_lines_starting_dash_space': len(lines) == 2 and all(l.startswith('- ') for l in lines)}
    if case_id == 'I03':
        return {'contains_both_requested_words': '같이' in text and '산책' in text}
    if case_id == 'I04':
        return {'two_numbered_lines': len(lines) == 2 and lines[0].startswith('1. ') and lines[1].startswith('2. ')}
    if case_id == 'I05':
        return {'starts_with_requested_word': text.startswith('오늘은'), 'forbidden_word_absent': '휴식' not in text}
    return {}

METRICS = ('generation_ms', 'generated_token_count', 'tokens_per_second',
           'peak_allocated_bytes', 'peak_reserved_bytes', 'incremental_allocated_bytes')

def measurements(events):
    return {metric: stats([e['result'][metric] for e in events]) for metric in METRICS}

def summary(run, scores=None):
    baseline = [e for e in run['results'].values() if e['phase'] == 'baseline' and e['status'] == 'success']
    rows = validate_scores(run, scores) if scores is not None else score_template(run)['rows']
    quality = {}
    for cat in CATEGORIES:
        selected = [r for r in rows if r['category'] == cat]
        graded = [r for r in selected if r['score'] is not None]
        quality[cat] = {'total': 5, 'eligible': sum(r['eligible'] for r in selected),
                        'graded': len(graded),
                        'mean': statistics.mean(r['score'] for r in graded) if graded else None}
    means = [quality[c]['mean'] for c in CATEGORIES]
    overall = statistics.mean(means) if all(m is not None for m in means) else None
    per_case = {}
    for case in run['dataset']['cases']:
        events = [e for e in baseline if e['case_id'] == case['id']]
        per_case[case['id']] = {'attempts': [run['results'].get(f'r{r}-{case["id"]}') for r in range(1, 4)],
                                'metrics': measurements(events),
                                'literal_checks': {e['attempt_id']: literal_checks(case['id'], e['result']['text']) for e in events},
                                'unique_decoded_outputs': len({e['result']['decoded_text_sha256'] for e in events})}
    return {'schema_version': 1, 'run_id': run['metadata']['run_id'],
            'execution_kind': run['metadata']['execution_kind'], 'created_at': now(),
            'dataset_sha256': run['metadata']['dataset_sha256'],
            'config_sha256': run['metadata']['config_sha256'],
            'code_sha256': run['metadata']['code_sha256'],
            'rubric_sha256': run['metadata']['rubric_sha256'],
            'raw_artifact_sha256': run['raw_artifact_sha256'],
            'schedule_complete': run['schedule_complete'], 'run_success': run['run_success'],
            'baseline_complete': run['baseline_complete'], 'supplements_complete': run['supplements_complete'],
            'journal_damaged': run['journal_damaged'], 'counts': run['counts'], 'load': run['load'],
            'cache_recovery_occurred': run['cache_recovery_occurred'],
            'baseline_metrics': measurements(baseline),
            'truncated_baseline_metrics': measurements([e for e in baseline if e['result']['termination'] == 'token_limit']),
            'pre_recovery_metrics': measurements([e for e in baseline if not e['after_recovery']]),
            'post_recovery_metrics': measurements([e for e in baseline if e['after_recovery']]),
            'warmup_metrics': measurements([e for e in run['results'].values() if e['phase'] == 'warmup' and e['status'] == 'success']),
            'supplement_metrics': measurements([e for e in run['results'].values() if e['phase'] == 'supplement' and e['status'] == 'success']),
            'per_case': per_case, 'quality': quality, 'overall_equal_category_mean': overall,
            'quality_complete': sum(q['graded'] for q in quality.values()) == 20,
            'human_judgments': rows,
            'limits': ['Generation latency excludes tokenization, decoding, file I/O and loading.',
                       'EOS counts toward throughput; allocator bytes exclude some driver/library memory.',
                       'Reserved cache reflects fixed ordering; three repeats do not establish significance.',
                       'Artifact hashes bind content; they do not authenticate execution or grader identity.']}

def prepare(path, output, scores_path=None, prepare_scores=False):
    run = load_run(path)
    # A destination inside the raw run would violate immutability.
    raw_path, dest = Path(path).resolve(), Path(output).resolve()
    require(dest != raw_path and raw_path not in dest.parents)
    scores_bytes = Path(scores_path).read_bytes() if scores_path else None
    scores = json.loads(scores_bytes) if scores_bytes is not None else None
    result = summary(run, scores)
    dest.mkdir(parents=True, exist_ok=False)
    if prepare_scores:
        write_json(dest / 'scores-template.json', score_template(run))
        write_json(dest / 'scoring-reference.json', {
            'execution_kind': run['metadata']['execution_kind'],
            'rubrics': run['dataset']['category_rubrics'],
            'cases': [{**c, 'first_round_result': run['results'].get('r1-' + c['id'])}
                      for c in run['dataset']['cases']]})
    else:
        result['scores_file_sha256'] = digest(scores_bytes) if scores_bytes is not None else None
        write_json(dest / 'summary.json', result)
    return result

class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise EvaluationError('ARGUMENT') from None

def main(argv=None):
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument('--run', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--scores', type=Path)
        group.add_argument('--prepare-scores', action='store_true')
        args = parser.parse_args(argv)
        result = prepare(args.run, args.output, args.scores, args.prepare_scores)
        print('검증 완료. 일정 완료 여부:', result['schedule_complete'],
              '/ 실행 종류:', result['execution_kind'])
        return 0
    except Exception:
        print('[ARTIFACT] 원본·채점 연결과 새 출력 경로를 확인하세요.', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
