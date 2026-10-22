"""Recompute the #55 prompt-experiment tables from committed files.

Usage: python3 score.py          # print summary
       python3 score.py --write  # rewrite summary.json
       python3 score.py --check  # exit 1 if summary.json differs
"""
import collections
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPERIMENTS = HERE.parents[1]
sys.path.insert(0, str(EXPERIMENTS / 'evaluation'))
import scoring  # noqa: E402

ISSUE37 = EXPERIMENTS / 'results' / '2026-10-17-issue37-model-comparison'
MODELS = ('qwen3-8b', 'qwen2.5-7b-instruct')
PROMPTS = ('base', 'guarded')
FIELDS = ('premise_accepted', 'fabricated', 'core_correct', 'over_refusal')


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def issue37_hashes(model, suite):
    folder = ISSUE37 / 'downloaded-results-001' / f'issue37-{model}-{suite}-001'
    hashes = {}
    for line in (folder / 'events.jsonl').read_text(encoding='utf-8').splitlines():
        event = json.loads(line)
        if event.get('type') != 'attempt_result':
            continue
        result = event.get('result', event)
        case_id, round_ = event.get('case_id', result.get('case_id')), event.get('round', result.get('round'))
        if round_ in (1, None) and case_id not in hashes:
            hashes[case_id] = result['generated_ids_sha256']
    return hashes


def main():
    rows = [json.loads(line) for line in (HERE / 'generations.jsonl').read_text(encoding='utf-8').splitlines()]
    cases = {s: {c['id']: c for c in load_json(EXPERIMENTS / 'evaluation' / f'{s}.json')['cases']}
             for s in ('record_qa', 'instructions')}
    blind = {b['bid']: b for b in load_json(HERE / 'judge' / 'blind-items.json')}
    key = load_json(HERE / 'judge' / 'blind-key.json')
    judged = load_json(HERE / 'judge' / 'judged.json')
    assert len(rows) == 368 and all(r['status'] == 'success' for r in rows)
    assert set(judged) == set(blind) == set(key)

    summary = {'generations': len(rows), 'terminations': dict(collections.Counter(r['termination'] for r in rows)),
               'conditions': {}}
    counts = collections.defaultdict(collections.Counter)
    for bid, item in blind.items():
        k, j = key[bid], judged[bid]
        group = item['type'] if item['type'] != 'human' else 'human_' + item['answerability']
        c = counts[(k['model'], k['prompt'], group)]
        c['n'] += 1
        for field in FIELDS:
            c[field] += bool(j[field])
        c['safe'] += not j['premise_accepted'] and not j['fabricated']

    def total(model, prompt, groups, field):
        return [sum(counts[(model, prompt, g)][field] for g in groups),
                sum(counts[(model, prompt, g)]['n'] for g in groups)]

    probe_risky = ('fp_confirm', 'fp_soft', 'no_record')
    human = ('human_answerable', 'human_partially_answerable', 'human_unanswerable')
    for model in MODELS:
        for prompt in PROMPTS:
            machine = {}
            for suite, fn in (('record_qa', scoring.qa), ('instructions', scoring.instructions)):
                scored = [fn(cases[suite][r['case_id']], r['text'], r['status']) for r in rows
                          if (r['model'], r['prompt'], r['suite']) == (model, prompt, suite)]
                machine[suite] = scoring.aggregate(scored, suite)['whole_accuracy']
            summary['conditions'][f'{model}|{prompt}'] = {
                'probe_safe': total(model, prompt, probe_risky, 'safe'),
                'probe_safe_by_type': {t: total(model, prompt, [t], 'safe') for t in probe_risky},
                'answerable_core_correct': total(model, prompt, ['answerable'], 'core_correct'),
                'over_refusal': total(model, prompt, ('answerable',) + human, 'over_refusal'),
                'human_safe': total(model, prompt, human, 'safe'),
                'record_qa_whole_accuracy': machine['record_qa'],
                'instructions_whole_accuracy': machine['instructions'],
            }

    reproduction = {}
    for model in MODELS:
        for suite in ('human', 'record_qa', 'instructions'):
            old = issue37_hashes(model, suite)
            new = {r['case_id']: r['generated_ids_sha256'] for r in rows
                   if (r['model'], r['prompt'], r['suite']) == (model, 'base', suite)}
            reproduction[f'{model}|{suite}'] = [sum(old.get(c) == h for c, h in new.items()), len(new)]
    summary['base_matches_issue37_round1'] = reproduction

    sheet_key = load_json(ISSUE37 / 'human-blind-001' / 'blind-key-001.json')['items']
    with (ISSUE37 / 'human-blind-001' / 'grading-sheet.csv').open(encoding='utf-8-sig') as handle:
        user = {(sheet_key[r['item']]['profile'], sheet_key[r['item']]['case_id']): int(r['grounding_1to5'])
                for r in csv.DictReader(handle)}
    pairs = []
    for bid, k in key.items():
        if k['suite'] == 'human' and k['prompt'] == 'base':
            unsafe = judged[bid]['premise_accepted'] or judged[bid]['fabricated']
            pairs.append((user[(k['model'], k['case_id'])], unsafe))
    summary['judge_vs_user_grounding'] = {
        'items': len(pairs),
        'agree_user_le_2': sum((u <= 2) == x for u, x in pairs),
        'agree_user_le_3': sum((u <= 3) == x for u, x in pairs),
    }

    text = json.dumps(summary, ensure_ascii=False, indent=1) + '\n'
    if '--write' in sys.argv:
        (HERE / 'summary.json').write_text(text, encoding='utf-8')
    elif '--check' in sys.argv:
        if (HERE / 'summary.json').read_text(encoding='utf-8') != text:
            print('MISMATCH')
            sys.exit(1)
        print('PASS')
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
