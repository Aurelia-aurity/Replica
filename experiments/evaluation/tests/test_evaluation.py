"""Boundary and artifact-integrity tests; synthetic execution only."""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import assets
import scoring
import human


def module(name):
    spec = importlib.util.spec_from_file_location('evaluation_' + name, ROOT / (name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


runner, reporter = module('run'), module('report')
collector, benchmark = module('collect'), module('kobest')


def value(text, cap=160, truncated=False):
    count = cap if truncated else 2
    return {'text': text, 'decoded_text_sha256': assets.digest(text.encode()),
            'output_empty': not text.strip(), 'generated_token_count': count,
            'generated_ids_excluding_terminal_eos': count if truncated else count - 1,
            'generated_ids_sha256': '0' * 64, 'termination': 'token_limit' if truncated else 'eos',
            'input_token_count': 10, 'generation_ms': 10.0, 'tokens_per_second': count * 100.0,
            'throughput_unavailable_reason': None, 'pre_allocated_bytes': 100,
            'pre_reserved_bytes': 200, 'peak_allocated_bytes': 150,
            'peak_reserved_bytes': 200, 'incremental_allocated_bytes': 50}


class Fake:
    def __init__(self, path, config):
        self.load_ms, self.environment = 1.0, {'mock': True}
        self.cases = assets.inputs(config['suite'])[1]['cases']
        self.by_prompt = {c['prompt']: c for c in self.cases}
    def generate(self, prompt, cap):
        c = self.by_prompt.get(prompt)
        text = '워밍업' if c is None else json.dumps(c['gold'], ensure_ascii=False) if 'gold' in c else c.get('passing_example_not_required_answer', c.get('acceptable_response_example'))
        return value(text, cap)


class Tests(unittest.TestCase):
    def test_release_and_witnesses(self):
        for suite in ('human', 'record_qa', 'instructions'):
            raw, data, release, config = assets.inputs(suite)
            self.assertEqual(len(data['cases']), 20)
            self.assertEqual(assets.snapshot_release(raw, release), data)
            with self.assertRaises(assets.EvaluationError):
                assets.snapshot_release(raw + b'\n', release)
            if suite == 'instructions':
                for c in data['cases']:
                    self.assertTrue(scoring.instructions(c, c['passing_example_not_required_answer'])['whole_success'])

    def test_qa_formats_and_partial(self):
        c = {'requested_keys': ['city', 'purpose'], 'gold': {'city': '전주', 'purpose': None}}
        for text in ['{"city":"전주","city":"전주","purpose":null}', '```json\n{}\n```',
                     '{"city":"전주","purpose":null,"extra":"x"}', '{"city":1,"purpose":null}',
                     '["전주",null]', '{"city":"전주","purpose":NaN}', '설명 {"city":"전주","purpose":null}',
                     '{"city":"전주"}', '{"city":{},"purpose":null}', '{"city":true,"purpose":null}']:
            r = scoring.qa(c, text)
            self.assertFalse(r['format_valid'], text)
            self.assertEqual(r['slots']['score'], 0)
        r = scoring.qa(c, '{"purpose":"null","city":" 전주 "}')
        self.assertEqual(r['slots']['score'], .5)
        self.assertFalse(r['whole_success'])
        self.assertEqual(scoring.qa(c, '{"city":"전주시","purpose":null}')['slots']['score'], .5)
        decomposed = unicodedata.normalize('NFD', '전주')
        self.assertTrue(scoring.qa(c, json.dumps({'city': decomposed, 'purpose': None}))['whole_success'])
        self.assertFalse(scoring.qa(c, '', 'failed')['whole_success'])
        self.assertEqual(scoring.qa(c, ' ')["state"], 'empty')

    def test_literal_lines_prefix_and_empty(self):
        def check(kind, val, text):
            return scoring.instructions({'kind': 'single', 'rules': [{'type': kind, 'value': val}]}, text)['whole_success']
        self.assertTrue(check('contains', '산책', '산책하기'))
        self.assertFalse(check('absent', '게임', '게임기'))
        self.assertTrue(check('contains', '산책', unicodedata.normalize('NFD', '산책')))
        self.assertFalse(check('prefix', '오늘은 ', ' 오늘은 시작'))
        self.assertFalse(check('prefix', '오늘은 ', '오늘은: 시작'))
        self.assertTrue(check('lines', 2, '가\r\n나\r\n'))
        self.assertFalse(check('lines', 2, '가\n나\n\n'))
        self.assertTrue(check('lines', 3, '가\n\n나'))
        self.assertFalse(check('absent', '게임', ' \n '))

    def test_equal_question_not_pooled_and_na(self):
        rows = [scoring.qa({'requested_keys': ['a'], 'gold': {'a': 'x'}}, '{"a":"x"}'),
                scoring.qa({'requested_keys': ['a', 'b', 'c'], 'gold': {'a': 'x', 'b': 'y', 'c': 'z'}}, '{}')]
        r = scoring.aggregate(rows, 'record_qa')
        self.assertEqual(r['slots']['score'], .5)
        self.assertEqual(r['slots']['correct_slots'], 1)
        self.assertEqual(r['slots']['eligible_slots'], 4)
        self.assertIsNone(r['null']['score'])

    def test_all_suites_mock_provenance_and_immutable(self):
        with tempfile.TemporaryDirectory() as t:
            for suite in ('human', 'record_qa', 'instructions'):
                path = Path(t) / suite
                self.assertEqual(runner.execute(suite, 'unused', path, Fake), 0)
                run = assets.load_run(path)
                before = {p.name: p.read_bytes() for p in path.iterdir()}
                r = reporter.report(run)
                self.assertEqual(r['execution_kind'], 'mock')
                self.assertEqual(r, reporter.report(assets.load_run(path)))
                self.assertTrue(r['schedule_complete'])
                self.assertEqual(r['operational']['successful_attempts'], 60)
                if suite != 'human':
                    self.assertEqual(r['three_round_mean']['whole_accuracy'], 1)
                else:
                    self.assertEqual(r['human']['pending'], 20)
                    self.assertIsNone(r['human']['metrics']['personal_fact_accuracy']['score'])
                self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})
                with self.assertRaises(FileExistsError):
                    runner.execute(suite, 'unused', path, Fake)

    def test_failed_attempt_and_supplement_not_replaced(self):
        class Fail(Fake):
            def __init__(self, p, c):
                super().__init__(p, c)
                self.i = 0
            def generate(self, prompt, cap):
                self.i += 1
                if self.i == 4:
                    raise assets.EvaluationError('GENERATE')
                v = super().generate(prompt, cap)
                return value(v['text'], cap, truncated=self.i == 5)
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / 'run'
            self.assertEqual(runner.execute('record_qa', 'unused', path, Fail), 1)
            run = assets.load_run(path)
            self.assertTrue(run['schedule_complete'])
            self.assertEqual(len(run['counts']['supplement']['success']), 1)
            r = reporter.report(run)
            self.assertEqual(r['rounds'][0]['scores']['whole_accuracy'], .95)
            self.assertEqual(r['operational']['successful_attempts'], 59)

    def test_integrity_duplicate_missing_hash_and_damaged_tail(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'run'
            runner.execute('instructions', 'unused', p, Fake)
            events_file = p / 'events.jsonl'
            original = events_file.read_bytes()
            events = [json.loads(x) for x in original.splitlines()]
            for mutate in ('hash', 'duplicate', 'missing'):
                bad = copy.deepcopy(events)
                if mutate == 'hash':
                    bad[4]['result']['text'] = 'tampered'
                elif mutate == 'duplicate':
                    bad.insert(5, copy.deepcopy(bad[4]))
                else:
                    bad.pop(4)
                events_file.write_bytes(b''.join(assets.canonical(e) + b'\n' for e in bad))
                with self.assertRaises(assets.EvaluationError):
                    assets.load_run(p)
            # Interrupted logs retain observed results but cannot manufacture
            # completion or a complete three-round mean.
            events_file.write_bytes(b''.join(assets.canonical(e) + b'\n' for e in events[:10]) + b'{')
            r = reporter.report(assets.load_run(p))
            self.assertFalse(r['schedule_complete'])
            self.assertIsNone(r['three_round_mean'])

    def test_human_labels_not_automatic_and_hash_binding(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'run'
            runner.execute('human', 'unused', p, Fake)
            run = assets.load_run(p)
            labels = human.template(run)
            for row in labels['rows']:
                row.update(review_complete=True, claims_review_complete=True,
                           unresolved_internal_contradiction=False, misleading_premise_handled=True,
                           notes='Synthetic annotation fixture; not a model evaluation.')
                for slot in row['known_slots']:
                    slot['label'] = 'correct'
                for slot in row['unknown_slots']:
                    slot['explicit_abstention'] = True
            r = human.aggregate(run, labels)
            self.assertTrue(r['complete'])
            self.assertEqual(r['whole_success']['score'], 1)
            self.assertIsNone(r['metrics']['personal_fact_accuracy']['score'])
            self.assertEqual(r['pairs']['misleading']['planned'], 5)
            labels['rows'][0]['source_response_hash'] = 'wrong'
            with self.assertRaises(assets.EvaluationError):
                human.aggregate(run, labels)

    def test_collection_pending_and_model_mismatch(self):
        with tempfile.TemporaryDirectory() as t:
            paths = {s: Path(t) / s for s in ('human', 'record_qa', 'instructions')}
            for s, p in paths.items():
                runner.execute(s, 'unused', p, Fake)
            result = collector.collect(paths)
            self.assertFalse(result['evaluation_data_complete'])
            self.assertIsNone(result['overall_composite_score'])
            self.assertEqual(result['kobest']['state'], 'pending')
            p = paths['record_qa'] / 'metadata.json'
            meta = json.loads(p.read_text())
            meta['config']['model_artifacts_sha256'] = {'different': 'weights'}
            meta['config_sha256'] = assets.digest(assets.canonical(meta['config']))
            p.write_text(json.dumps(meta))
            with self.assertRaises(assets.EvaluationError):
                collector.collect(paths)

    def test_human_overlap_ratios_failure_and_pending(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'run'
            runner.execute('human', 'unused', p, Fake)
            run = assets.load_run(p)
            labels = human.template(run)
            for row in labels['rows']:
                row.update(review_complete=True, claims_review_complete=True,
                           unresolved_internal_contradiction=False, misleading_premise_handled=True,
                           notes='Synthetic metric boundary fixture')
                for s in row['known_slots']:
                    s['label'] = 'correct'
                for s in row['unknown_slots']:
                    s['explicit_abstention'] = True
            labels['rows'][0]['claims'] = [
                {'claim': 'supported fixture', 'answer_span': 'fixture', 'label': 'supported', 'mode': 'asserted'},
                {'claim': 'contradicted fixture', 'answer_span': 'fixture', 'label': 'contradicted', 'mode': 'speculative'}]
            labels['rows'][0]['known_slots'][0]['label'] = 'over_abstained'
            r = human.aggregate(run, labels)
            self.assertEqual(r['metrics']['personal_fact_accuracy']['score'], .5)
            self.assertEqual(r['metrics']['personal_fact_accuracy']['eligible_cases'], 1)
            self.assertEqual(r['metrics']['unsupported_response_rate']['score'], .05)
            self.assertEqual(r['metrics']['record_contradiction_rate']['score'], .05)
            self.assertAlmostEqual(r['metrics']['required_information_coverage']['score'], (16 + 2/3) / 17)
            self.assertAlmostEqual(r['metrics']['over_abstention']['score'], (1/3) / 17)
            self.assertEqual(r['metrics']['justified_abstention']['score'], 1)
            self.assertEqual(r['whole_success']['score'], .95)
            self.assertEqual(r['pairs']['paraphrase']['success_rate'], .8)
            labels['rows'][1]['review_complete'] = False
            partial = human.aggregate(run, labels)
            self.assertFalse(partial['complete'])
            self.assertEqual(partial['pending'], 1)
            self.assertEqual(partial['pairs']['paraphrase']['pending'], 0)
            self.assertEqual(partial['pairs']['paraphrase']['success_rate'], .8)
            failed = copy.deepcopy(run)
            aid = 'r1-' + failed['dataset']['cases'][0]['id']
            failed['results'][aid] = dict(failed['results'][aid], status='failed')
            labels = human.template(failed)
            r = human.aggregate(failed, labels)
            self.assertEqual(r['failed_or_empty'], 1)
            self.assertEqual(r['pending'], 19)
            self.assertFalse(r['whole_success']['by_case'][failed['dataset']['cases'][0]['id']])
            self.assertIsNone(r['metrics']['unsupported_response_rate']['score'])

    def test_benchmark_full_coverage_finite_and_no_failed_wrong(self):
        lock = assets.read_json(ROOT / 'kobest-lock.json')
        result = {'samples': {}, 'results': {}}
        for name in lock['tasks']:
            count = lock['test_files'][name.removeprefix('kobest_')]['test_count']
            choices = 4 if name == 'kobest_hellaswag' else 2
            result['samples'][name] = [{'doc_id': i, 'resps': [[(-1.0, False)]] * choices, 'acc': 1, 'acc_norm': 1} for i in range(count)]
            result['results'][name] = {'acc,none': 1.0, 'acc_norm,none': 1.0}
        self.assertEqual(benchmark.validate_result(result, lock)['project_equal_task_mean'], 1)
        for mutation in ('missing_choice', 'missing_acc_norm', 'negative_acc_norm', 'invalid_sample_acc'):
            bad = copy.deepcopy(result)
            name = 'kobest_hellaswag'
            if mutation == 'missing_choice':
                bad['samples'][name][0]['resps'] = bad['samples'][name][0]['resps'][:1]
            elif mutation == 'missing_acc_norm':
                bad['results'][name].pop('acc_norm,none')
            elif mutation == 'negative_acc_norm':
                bad['results'][name]['acc_norm,none'] = -50
            else:
                bad['samples'][name][0]['acc'], bad['samples'][name][1]['acc'] = 2, 0
            with self.assertRaises((assets.EvaluationError, KeyError)):
                benchmark.validate_result(bad, lock)
        name = lock['tasks'][0]
        result['samples'][name][0]['resps'][0][0] = (float('nan'), False)
        with self.assertRaises(assets.EvaluationError):
            benchmark.validate_result(result, lock)
        result['samples'][name].pop(0)
        with self.assertRaises(assets.EvaluationError):
            benchmark.validate_result(result, lock)

    def test_package_overlay_and_priority_identity(self):
        class Dist:
            def __init__(self, version, path):
                self.metadata, self.version, self.path = {'Name': 'Review_Dependency'}, version, path
            def locate_file(self, unused):
                return Path(self.path)
        original = [Dist('2.0', '/private/tmp/venv'), Dist('1.0', '/private/tmp/system')]
        same = [Dist('2.0', '/private/tmp/venv'), Dist('1.0', '/private/tmp/system')]
        changed = [Dist('3.0', '/private/tmp/venv'), Dist('1.0', '/private/tmp/system')]
        self.assertEqual(benchmark.package_inventory(original), benchmark.package_inventory(same))
        self.assertNotEqual(benchmark.package_inventory(original), benchmark.package_inventory(changed))
        self.assertNotEqual(benchmark.package_inventory(original), benchmark.package_inventory(list(reversed(original))))
        self.assertEqual(len(benchmark.package_inventory(original)), 2)

    def test_label_file_snapshot_hash_during_mutation(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'run'
            runner.execute('human', 'unused', p, Fake)
            run = assets.load_run(p)
            labels = human.template(run)
            for row in labels['rows']:
                row.update(review_complete=True, claims_review_complete=True,
                           unresolved_internal_contradiction=False, misleading_premise_handled=True, notes='Synthetic label hash fixture')
                for s in row['known_slots']:
                    s['label'] = 'correct'
                for s in row['unknown_slots']:
                    s['explicit_abstention'] = True
            label_path = Path(t) / 'labels.json'
            original = json.dumps(labels).encode()
            label_path.write_bytes(original)
            calculate = reporter.report
            def mutate(run, parsed):
                replacement = copy.deepcopy(labels)
                replacement['rows'][0]['known_slots'][0]['label'] = 'incorrect'
                label_path.write_text(json.dumps(replacement))
                return calculate(run, parsed)
            with patch.object(reporter, 'report', side_effect=mutate):
                result = reporter.report_with_label_file(run, label_path)
            self.assertEqual(result['labels_sha256'], assets.digest(original))
            self.assertEqual(result['human']['whole_success']['score'], 1)
            self.assertEqual(calculate(run, json.loads(label_path.read_bytes()))['human']['whole_success']['score'], .95)


if __name__ == '__main__':
    unittest.main()
