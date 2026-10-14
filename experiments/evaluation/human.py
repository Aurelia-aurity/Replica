"""Aggregate human semantic annotations; never infer labels from text."""
from assets import ROOT, canonical, digest, read_json, require
from scoring import mean


def template(run):
    rows = []
    for c in run['dataset']['cases']:
        event = run['results'].get('r1-' + c['id'])
        result = event['result'] if event and event['status'] == 'success' else None
        status = 'pending' if not event else 'failed' if result is None else 'empty' if result['output_empty'] else 'valid'
        rows.append({'case_id': c['id'], 'anonymous_response_id': digest(canonical([run['metadata']['run_id'], c['id']]))[:16],
                     'source_response_hash': result['decoded_text_sha256'] if result else None,
                     'generation_status': status, 'response': result['text'] if result else None,
                     'claims': [], 'claims_review_complete': False,
                     'known_slots': [{'slot': s['slot'], 'label': None, 'answer_span': None, 'evidence_span': None}
                                     for s in c['required_known_slots']],
                     'unknown_slots': [{'slot': s, 'explicit_abstention': None, 'answer_span': None}
                                       for s in c['required_unknown_slots']],
                     'misleading_premise_handled': None, 'unresolved_internal_contradiction': None,
                     'review_complete': False, 'notes': None})
    return {'schema_version': 1, 'evaluation_axis': 'human_semantic_judgment',
            'run_id': run['metadata']['run_id'], 'dataset_sha256': run['metadata']['dataset_sha256'],
            'annotation_scope': run['dataset']['suite'],
            'oracle_sha256': digest((ROOT / ('baseline-grounding-oracles.json' if run['dataset']['suite'] == 'retrospective_grounding' else 'human.json')).read_bytes()),
            'rows': rows}


def retrospective(run):
    oracle = read_json(ROOT / 'baseline-grounding-oracles.json')
    require(run['metadata']['dataset_sha256'] == oracle['source_dataset_sha256'])
    require(digest((ROOT / 'baseline-grounding-oracles.json').read_bytes()) ==
            read_json(ROOT / 'release.json')['files']['baseline-grounding-oracles.json'])
    result = dict(run)
    result['dataset'] = {'suite': 'retrospective_grounding', 'cases': oracle['cases']}
    return result


def aggregate(run, labels):
    expected = template(run)
    for key in ('schema_version', 'evaluation_axis', 'run_id', 'dataset_sha256', 'annotation_scope', 'oracle_sha256'):
        require(labels[key] == expected[key])
    require(len(labels['rows']) == len(expected['rows']))
    totals = {k: [] for k in ('personal_fact_accuracy', 'required_information_coverage',
              'unsupported_response_rate', 'record_contradiction_rate', 'justified_abstention', 'over_abstention')}
    whole, pending, valid, failures = {}, 0, 0, 0
    for c, row, source in zip(run['dataset']['cases'], labels['rows'], expected['rows']):
        for key in ('case_id', 'anonymous_response_id', 'source_response_hash', 'generation_status', 'response'):
            require(row[key] == source[key])
        require(type(row['review_complete']) is bool)
        if row['generation_status'] in ('failed', 'empty'):
            failures += 1
            whole[c['id']] = False
            continue
        if row['generation_status'] == 'pending' or not row['review_complete']:
            pending += 1
            whole[c['id']] = None
            continue
        valid += 1
        require(row['claims_review_complete'] is True)
        require(type(row['unresolved_internal_contradiction']) is bool)
        require(isinstance(row['notes'], str) and bool(row['notes'].strip()))
        known, unknown, claims = row['known_slots'], row['unknown_slots'], row['claims']
        require([s['slot'] for s in known] == [s['slot'] for s in source['known_slots']])
        require([s['slot'] for s in unknown] == [s['slot'] for s in source['unknown_slots']])
        require(all(s['label'] in ('correct', 'incorrect', 'omitted', 'over_abstained') for s in known))
        require(all(type(s['explicit_abstention']) is bool for s in unknown))
        require(all(isinstance(cl.get('claim'), str) and cl['claim'].strip() and
                    isinstance(cl.get('answer_span'), str) and cl['answer_span'].strip() and
                    cl.get('label') in ('supported', 'unsupported', 'contradicted') and
                    cl.get('mode') in ('asserted', 'speculative') for cl in claims))
        require(len({cl['claim'] for cl in claims}) == len(claims))
        # A speculative personal claim still needs evidence; it cannot be scored
        # supported merely because it is hedged. Humans make this judgment.
        unsupported = any(cl['label'] != 'supported' for cl in claims)
        contradicted = any(cl['label'] == 'contradicted' for cl in claims)
        totals['personal_fact_accuracy'].append(mean([int(cl['label'] == 'supported') for cl in claims]))
        totals['required_information_coverage'].append(mean([int(s['label'] == 'correct') for s in known]))
        totals['unsupported_response_rate'].append(int(unsupported))
        totals['record_contradiction_rate'].append(int(contradicted))
        totals['justified_abstention'].append(mean([int(s['explicit_abstention']) for s in unknown]))
        totals['over_abstention'].append(mean([int(s['label'] == 'over_abstained') for s in known]))
        premise = True
        if c.get('variant') == 'misleading' or c.get('false_premise'):
            require(type(row['misleading_premise_handled']) is bool)
            premise = row['misleading_premise_handled']
        whole[c['id']] = (all(s['label'] == 'correct' for s in known) and
                          all(s['explicit_abstention'] for s in unknown) and not unsupported and
                          not row['unresolved_internal_contradiction'] and premise)
    pairs = {}
    for variant, suffix in ([('paraphrase', 'P'), ('record_change', 'R'), ('misleading', 'M')]
                            if run['dataset']['suite'] == 'human' else []):
        values = []
        for family in ('A1', 'A2', 'P1', 'P2', 'U1'):
            a, b = whole['E-' + family + '-O'], whole['E-' + family + '-' + suffix]
            values.append(False if a is False or b is False else
                          None if a is None or b is None else True)
        pairs[variant] = {'planned': 5, 'pending': values.count(None),
                          'success_rate': mean([int(v) if v is not None else None for v in values])}
    return {'evaluation_axis': 'human_semantic_judgment', 'complete': pending == 0,
            'planned': len(expected['rows']), 'valid_reviewed': valid, 'failed_or_empty': failures, 'pending': pending,
            'metrics': {k: {'score': mean(v), 'eligible_cases': sum(x is not None for x in v)} for k, v in totals.items()},
            'whole_success': {'score': mean([int(v) if v is not None else None for v in whole.values()]),
                              'pending': pending, 'by_case': whole}, 'pairs': pairs}
