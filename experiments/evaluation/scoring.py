"""Deterministic literal scoring. No language model or semantic judge."""
import json
import statistics
import unicodedata


def nfc(text):
    return unicodedata.normalize('NFC', text)


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def strict_object(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('non_json_constant')
    obj = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if type(obj) is not dict or any(v is not None and type(v) is not str for v in obj.values()):
        raise ValueError('value_type')
    return obj


def qa(case, text, status='success'):
    valid = False
    actual = {}
    state = 'execution_failed' if status != 'success' else 'empty' if not text.strip() else 'format_invalid'
    if status == 'success' and text.strip():
        try:
            actual = strict_object(text)
            valid = set(actual) == set(case['requested_keys'])
        except (ValueError, TypeError, RecursionError):
            pass
    correct = {}
    for key, gold in case['gold'].items():
        val = actual.get(key)
        correct[key] = bool(valid and ((val is None and gold is None) or
                           (isinstance(val, str) and isinstance(gold, str) and
                            nfc(val).strip() == nfc(gold).strip())))
    def subgroup(keys):
        return {'correct': sum(correct[k] for k in keys), 'eligible': len(keys),
                'score': mean([int(correct[k]) for k in keys])}
    all_slots = subgroup(list(correct))
    whole = valid and all(correct.values())
    if valid:
        state = 'correct' if whole else 'content_wrong'
    return {'state': state, 'whole_success': whole, 'format_valid': valid,
            'slots': all_slots, 'known': subgroup([k for k, v in case['gold'].items() if v is not None]),
            'null': subgroup([k for k, v in case['gold'].items() if v is None])}


def instructions(case, text, status='success'):
    text = nfc(text)
    lines = text.replace('\r\n', '\n').replace('\r', '\n')
    if lines.endswith('\n'):
        lines = lines[:-1]
    valid = status == 'success' and bool(text.strip())
    passed = []
    for r in case['rules']:
        kind, value = r['type'], r['value']
        if kind == 'lines':
            ok = len(lines.split('\n')) == value
        elif kind == 'contains':
            ok = nfc(value) in text
        elif kind == 'absent':
            ok = nfc(value) not in text
        elif kind == 'prefix':
            ok = text.startswith(nfc(value))
        else:
            raise ValueError('unknown_rule')
        passed.append(bool(valid and ok))
    whole = all(passed)
    return {'whole_success': whole, 'rules_passed': passed, 'kind': case['kind'],
            'state': 'execution_failed' if status != 'success' else 'empty' if not valid else
                     'correct' if whole else 'content_wrong'}


def aggregate(rows, suite):
    if suite == 'record_qa':
        result = {'whole_accuracy': mean([int(r['whole_success']) for r in rows]),
                  'format_validity': mean([int(r['format_valid']) for r in rows])}
        for group in ('slots', 'known', 'null'):
            eligible = [r[group] for r in rows if r[group]['eligible']]
            result[group] = {'score': mean([r['score'] for r in eligible]),
                             'correct_slots': sum(r['correct'] for r in eligible),
                             'eligible_slots': sum(r['eligible'] for r in eligible),
                             'eligible_questions': len(eligible)}
        return result
    return {'whole_accuracy': mean([int(r['whole_success']) for r in rows]),
            'rule_accuracy': mean([int(p) for r in rows for p in r['rules_passed']]),
            'single_accuracy': mean([int(r['whole_success']) for r in rows if r['kind'] == 'single']),
            'compound_accuracy': mean([int(r['whole_success']) for r in rows if r['kind'] == 'compound'])}
