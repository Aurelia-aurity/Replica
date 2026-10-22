"""Recompute the #37 comparison table from published files (standard library only)."""
import csv
import hashlib
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = HERE.parents[1] / 'evaluation'
sys.path.insert(0, str(EVAL))
import scoring  # noqa: E402

PROFILES = ['qwen2.5-7b-instruct', 'ax-4.0-light', 'ax-3.1-light', 'qwen3-8b',
            'qwen3-4b-instruct-2507', 'kanana-1.5-8b-instruct-2505']
AREAS = ['naturalness', 'instructions', 'usefulness', 'grounding']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main_attempts(run):
    events = [json.loads(line) for line in (run / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    return [e for e in events if e['type'] == 'attempt_result' and e.get('phase') == 'baseline']


def machine(profile, suite):
    cases = {c['id']: c for c in json.loads((EVAL / f'{suite}.json').read_text(encoding='utf-8'))['cases']}
    check = scoring.qa if suite == 'record_qa' else scoring.instructions
    attempts = main_attempts(HERE / 'downloaded-results-001' / f'issue37-{profile}-{suite}-001')
    assert len(attempts) == 60 and all(e['status'] == 'success' for e in attempts)
    return sum(check(cases[e['case_id']], e['result']['text'])['whole_success'] for e in attempts) / 60


def main():
    published = json.loads((HERE / 'published-files.json').read_text(encoding='utf-8'))
    for name, digest in published.items():
        assert sha(HERE / name) == digest, name
    key = json.loads((HERE / 'human-blind-001/blind-key-001.json').read_text(encoding='utf-8'))['items']
    rows = list(csv.DictReader((HERE / 'human-blind-001/grading-sheet.csv').open(encoding='utf-8-sig')))
    assert len(rows) == 120
    table = {}
    for p in PROFILES:
        mine = [r for r in rows if key[r['item']]['profile'] == p]
        human = {a: statistics.mean(int(r[a + '_1to5']) for r in mine) for a in AREAS}
        kobest = json.loads((HERE / 'downloaded-results-001' / f'issue37-{p}-kobest-001/report.json').read_text())
        assert kobest['complete_full_test'] is True
        table[p] = {'record_qa_whole': machine(p, 'record_qa'), 'instructions_whole': machine(p, 'instructions'),
                    'kobest_mean': kobest['project_equal_task_mean'],
                    **{'human_' + a: v for a, v in human.items()},
                    'human_mean4': statistics.mean(human.values())}
    expected = json.loads((HERE / 'comparison.json').read_text(encoding='utf-8'))['models']
    for p, values in table.items():
        for k, v in values.items():
            assert abs(expected[p][k] - v) < 1e-9, (p, k)
    for p, v in table.items():
        print(f"{p:30s} QA {v['record_qa_whole']:.1%}  instr {v['instructions_whole']:.1%}  "
              f"KoBEST {v['kobest_mean']:.2%}  human {v['human_mean4']:.2f} (grounding {v['human_grounding']:.2f})")
    print('PASS: published hashes, machine re-scoring, KoBEST reports, blind human aggregation.')


if __name__ == '__main__':
    main()
