"""Regrade published synthetic logs and validate numeric benchmark evidence.

This verifies file consistency and calculations, not semantic label validity,
independent human assessment, or the unpublished original benchmark result.
"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / 'evaluation'))
from assets import baseline_module, load_run, read_json
import human
from report import report_with_label_file
from kobest import validate_result


def verify():
    manifest = read_json(ROOT / 'published-files.json')
    expected = manifest['files']
    actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.name != 'published-files.json'}
    assert actual == set(expected), 'Published file set differs'
    for name, sha in expected.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha, name
    provenance = read_json(ROOT / 'provenance.json')
    for name, sha in provenance['byte_identical_source_artifacts'].items():
        assert expected[name] == sha, name
    ratings = read_json(ROOT / 'human-ratings-002.json')
    source = (ROOT / ratings['source_document']).read_bytes()
    assert hashlib.sha256(source).hexdigest() == ratings['source_sha256']
    assert len(ratings['rows']) == 20
    assert sum(r['score'] for r in ratings['rows']) == 76
    assert ratings['summary']['mean'] == 3.8
    collected = read_json(ROOT / 'collected-002/report.json')
    fingerprints = []
    for suite, run_name, label_path, report_path in [
        ('human', 'issue36-human-001', 'semantic-reviewed-001/human-labels.json', 'human-semantic-scoring-001/report.json'),
        ('record_qa', 'issue36-qa-001', None, 'downloaded-results-001/issue36-qa-scoring-001/report.json'),
        ('instructions', 'issue36-instructions-001', None, 'downloaded-results-001/issue36-instructions-scoring-001/report.json'),
    ]:
        run = load_run(ROOT / 'downloaded-results-001' / run_name)
        assert run['dataset']['suite'] == suite
        fingerprints.append(run['metadata']['config']['model_artifacts_sha256'])
        regenerated = report_with_label_file(run, ROOT / label_path if label_path else None)
        assert regenerated == read_json(ROOT / report_path), suite
        embedded = (collected['human_semantic_quality'] if suite == 'human'
                    else collected['deterministic_mechanical'][suite])
        assert regenerated == embedded, suite
    assert all(v == fingerprints[0] for v in fingerprints)
    assert collected['model_artifacts_sha256'] == fingerprints[0]
    old_run = baseline_module('report').load_run(ROOT / 'source/issue33-baseline-run-001')
    old_report = report_with_label_file(human.retrospective(old_run), ROOT / 'retrospective-semantic-reviewed-001/human-labels.json')
    assert old_report == read_json(ROOT / 'retrospective-semantic-scoring-001/report.json')
    benchmark = ROOT / 'downloaded-results-001/issue36-kobest-001'
    identity = read_json(benchmark / 'identity.json')
    summary = read_json(benchmark / 'report.json')
    assert hashlib.sha256((benchmark / 'identity.json').read_bytes()).hexdigest() == summary['identity_sha256']
    assert identity['model_sha256'] == fingerprints[0]
    lock = read_json(ROOT.parents[1] / 'evaluation/kobest-lock.json')
    assert identity['lock'] == lock
    projection = read_json(ROOT / 'kobest-sample-metrics.json')
    assert projection['projection']['source_sha256'] == summary['result_sha256']
    assert provenance['omitted_original_kobest_result']['sha256'] == summary['result_sha256']
    result = validate_result(projection, lock)
    assert all(summary[key] == value for key, value in result.items())
    assert collected['kobest']['report'] == summary
    assert collected['evaluation_data_complete'] is True
    assert collected['overall_composite_score'] is None
    print('PASS: file hashes, user score sum, 3 generation regrades, old G01–G05, 4,561 benchmark sample metrics.')
    print('Semantic validity and unpublished original KoBEST bytes require separate evidence; no service acceptance claim.')


if __name__ == '__main__':
    verify()
