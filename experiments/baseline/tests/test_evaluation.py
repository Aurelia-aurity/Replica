"""Mock-only contract tests. No measured GPU performance or inferred grades."""
import copy
import json
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import run
import report

def value(text='답변', cap=160, truncated=False):
    count = cap if truncated else 2
    ids = [1] * count if truncated else [1, 2]
    return {'text': text, 'decoded_text_sha256': common.digest(text.encode()),
            'output_empty': not text.strip(), 'generated_token_count': count,
            'generated_ids_excluding_terminal_eos': count if truncated else count - 1,
            'generated_ids_sha256': common.digest(common.canonical(ids)),
            'termination': 'token_limit' if truncated else 'eos',
            'input_token_count': 10, 'generation_ms': 10.0, 'tokens_per_second': count * 100.0,
            'throughput_unavailable_reason': None, 'pre_allocated_bytes': 100,
            'pre_reserved_bytes': 200, 'peak_allocated_bytes': 150,
            'peak_reserved_bytes': 200, 'incremental_allocated_bytes': 50}

class Fake:
    def __init__(self, path, config):
        self.load_ms, self.environment, self.index = 1.0, {'mock': True}, 0
    def generate(self, prompt, cap):
        self.index += 1
        return value('' if self.index == 4 else '답변', cap,
                     truncated=self.index == 5)

class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / 'raw'
    def tearDown(self):
        self.tmp.cleanup()
    def execute(self, factory=Fake):
        code = run.execute('unused', self.raw, factory)
        return code, report.load_run(self.raw)
    def mutate_events(self, mutation):
        p = self.raw / 'events.jsonl'
        events = [json.loads(x) for x in p.read_text().splitlines()]
        mutation(events)
        p.write_bytes(b''.join(common.canonical(e) + b'\n' for e in events))
    def test_confirmed_exact_release(self):
        raw, dataset, release, config = common.inputs()
        self.assertEqual(len(dataset['cases']), 20)
        with self.assertRaises(common.EvaluationError):
            common.validate_release(raw + b'\n', release)
        altered = dict(release, confirmation_status='draft')
        with self.assertRaises(common.EvaluationError):
            common.validate_release(raw, altered)
    def test_schedule_supplement_and_empty(self):
        code, result = self.execute()
        self.assertEqual(code, 0)
        self.assertTrue(result['run_success'])
        self.assertEqual(len(result['counts']['baseline']['success']), 60)
        self.assertEqual(result['counts']['supplement']['success'], ['s-r1-N02'])
        self.assertTrue(report.score_template(result)['rows'][0]['eligible'])
        self.assertTrue(result['results']['r1-N01']['result']['output_empty'])
        self.assertEqual(list(result['results'])[3:23], ['r1-' + c['id'] for c in result['dataset']['cases']])
        self.assertEqual(report.summary(result)['baseline_metrics']['generation_ms']['n'], 60)
    def test_zero_duration_and_eos_at_cap(self):
        v = value(truncated=True)
        v.update(termination='eos', generated_ids_excluding_terminal_eos=159,
                 generation_ms=0.0, tokens_per_second=None, throughput_unavailable_reason='zero_duration')
        common.validate_success(v, 160)
        v['generation_ms'] = float('nan')
        with self.assertRaises(common.EvaluationError):
            common.validate_success(v, 160)
    def test_read_only_and_destination_protection(self):
        self.execute()
        before = {p.name: p.read_bytes() for p in self.raw.iterdir()}
        report.prepare(self.raw, self.root / 'scoring', prepare_scores=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.raw.iterdir()})
        for dest in (self.raw / 'scoring', self.root / 'scoring'):
            with self.assertRaises(Exception):
                report.prepare(self.raw, dest, prepare_scores=True)
        with self.assertRaises(FileExistsError):
            run.execute('unused', self.raw, Fake)
    def test_scoring_coverage_binding(self):
        _, result = self.execute()
        scores = report.score_template(result)
        scores['graded_at'] = common.now()
        for index in (0, 5, 10):
            scores['rows'][index].update(score=4, reason='수동 테스트 예시', fabricated_personal_fact=False)
        self.assertIsNone(report.summary(result, scores)['overall_equal_category_mean'])
        scores['rows'][15].update(score=2, reason='수동 테스트 예시', fabricated_personal_fact=True)
        summary = report.summary(result, scores)
        self.assertEqual(summary['overall_equal_category_mean'], 3.5)
        self.assertFalse(summary['quality_complete'])
        scores['rows'][0]['decoded_text_sha256'] = 'bad'
        with self.assertRaises(common.EvaluationError):
            report.validate_scores(result, scores)
    def test_score_reject_boolean_and_missing_reason(self):
        _, result = self.execute()
        for score, reason in ((True, 'reason'), (6, 'reason'), (1, '')):
            scores = report.score_template(result)
            scores['graded_at'] = common.now()
            scores['rows'][0].update(score=score, reason=reason, fabricated_personal_fact=False)
            with self.assertRaises(common.EvaluationError):
                report.validate_scores(result, scores)
    def test_recovery_no_retry_and_separate_metrics(self):
        class Recover(Fake):
            def generate(self, prompt, cap):
                if self.index == 3:
                    self.index += 1
                    raise common.EvaluationError('OOM', recovered=True)
                return super().generate(prompt, cap)
        code, result = self.execute(Recover)
        self.assertEqual(code, 1)
        self.assertTrue(result['schedule_complete'])
        self.assertEqual(result['counts']['baseline']['failed'], ['r1-N01'])
        self.assertFalse(report.score_template(result)['rows'][0]['eligible'])
        self.assertEqual(report.summary(result)['post_recovery_metrics']['generation_ms']['n'], 59)
    def test_warmup_failure_stops(self):
        class Fail(Fake):
            def generate(self, prompt, cap):
                raise common.EvaluationError('INPUT')
        code, result = self.execute(Fail)
        self.assertEqual(code, 1)
        self.assertEqual(len(result['counts']['baseline']['unexecuted']), 60)
    def test_fatal_and_load_failure(self):
        class Fatal(Fake):
            def generate(self, prompt, cap):
                if self.index == 3:
                    raise common.EvaluationError('GENERATE', fatal=True)
                return super().generate(prompt, cap)
        _, result = self.execute(Fatal)
        self.assertEqual(len(result['counts']['baseline']['failed']), 1)
        self.assertEqual(len(result['counts']['baseline']['unexecuted']), 59)
    def test_load_failure(self):
        def fail(path, config):
            raise common.EvaluationError('LOAD', fatal=True)
        code, result = self.execute(fail)
        self.assertEqual(code, 1)
        self.assertEqual(result['load']['status'], 'failed')
        self.assertEqual(len(result['counts']['baseline']['unexecuted']), 60)
    def test_fatal_last_baseline_accounting(self):
        class Last(Fake):
            def generate(self, prompt, cap):
                if self.index == 62:
                    raise common.EvaluationError('GENERATE', fatal=True)
                self.index += 1
                return value()
        code, result = self.execute(Last)
        self.assertEqual(code, 1)
        self.assertTrue(result['baseline_complete'])
        self.assertFalse(result['run_success'])
    def test_keyboard_interrupt_retains_started_attempt(self):
        class Interrupt(Fake):
            def generate(self, prompt, cap):
                if self.index == 3:
                    raise KeyboardInterrupt
                return super().generate(prompt, cap)
        code, result = self.execute(Interrupt)
        self.assertEqual(code, 1)
        self.assertEqual(result['counts']['baseline']['interrupted'], ['r1-N01'])
    def test_interrupt_after_last_result_matches_completed_slots(self):
        class Plain(Fake):
            def generate(self, prompt, cap): return value()
        original = run.Journal.append
        def interrupt(journal, kind, **fields):
            original(journal, kind, **fields)
            if kind == 'attempt_result' and fields['attempt_id'] == 'r3-G05':
                raise KeyboardInterrupt
        with patch.object(run.Journal, 'append', interrupt):
            code = run.execute('unused', self.raw, Plain)
        result = report.load_run(self.raw)
        self.assertEqual(code, 1)
        self.assertTrue(result['schedule_complete'])
        self.assertFalse(result['run_success'])
        self.assertEqual(len(result['counts']['baseline']['success']), 60)
    def test_partial_result_interrupt_does_not_append_terminal(self):
        original = run.Journal.__init__
        class PartialWriter:
            def __init__(self, handle): self.handle = handle
            def __getattr__(self, name): return getattr(self.handle, name)
            def write(self, text):
                if '"attempt_id":"r1-N01"' in text and '"type":"attempt_result"' in text:
                    self.handle.write(text[:80])
                    self.handle.flush()
                    raise KeyboardInterrupt
                return self.handle.write(text)
        def install(journal, path, run_id):
            original(journal, path, run_id)
            journal.handle = PartialWriter(journal.handle)
        with patch.object(run.Journal, '__init__', install):
            code = run.execute('unused', self.raw, Fake)
        result = report.load_run(self.raw)
        self.assertEqual(code, 1)
        self.assertTrue(result['journal_damaged'])
        self.assertFalse((self.raw / 'events.jsonl').read_bytes().endswith(b'\n'))
        self.assertEqual(result['counts']['baseline']['interrupted'], ['r1-N01'])
    def test_real_signal_during_terminal_fsync_does_not_duplicate(self):
        original_append, original_fsync = run.Journal.append, run.os.fsync
        terminal = [False]
        def append(journal, kind, **fields):
            terminal[0] = kind == 'run_end'
            try: return original_append(journal, kind, **fields)
            finally: terminal[0] = False
        def fsync(fd):
            original_fsync(fd)
            if terminal[0]: signal.raise_signal(signal.SIGINT)
        with patch.object(run.Journal, 'append', append), patch.object(run.os, 'fsync', fsync):
            code = run.execute('unused', self.raw, Fake)
        result = report.load_run(self.raw)
        self.assertEqual(code, 1)  # process interrupted after terminal commit
        self.assertTrue(result['run_success'])
        self.assertTrue(result['schedule_complete'])
        self.assertEqual((self.raw / 'events.jsonl').read_text().count('"type":"run_end"'), 1)
    def test_approved_config_identity_required_even_with_updated_hash(self):
        self.execute()
        meta_path = self.raw / 'metadata.json'
        original = meta_path.read_bytes()
        for key, altered in (('model_id', 'Different/Model-1B'), ('baseline_reference_commit', '0' * 40)):
            meta = json.loads(original)
            meta['config'][key] = altered
            meta['config_sha256'] = common.digest(common.canonical(meta['config']))
            meta_path.write_bytes(common.canonical(meta))
            with self.assertRaises(common.EvaluationError): report.load_run(self.raw)
            alternate = self.root / ('input-' + key)
            alternate.mkdir()
            for name in ('cases.json', 'dataset-release.json'):
                (alternate / name).write_bytes((common.ROOT / name).read_bytes())
            (alternate / 'config.json').write_bytes(common.canonical(meta['config']))
            with patch.object(common, 'ROOT', alternate):
                with self.assertRaises(common.EvaluationError): common.inputs()
    def test_raw_hashes_match_parsed_snapshot_during_file_mutation(self):
        self.execute()
        original = {name: (self.raw / name).read_bytes() for name in ('metadata.json', 'cases.json', 'events.jsonl')}
        original_validate = report.validate_success
        changed = [False]
        def mutate(value, cap):
            original_validate(value, cap)
            if not changed[0]:
                changed[0] = True
                for name in original:
                    (self.raw / name).write_bytes(b'changed-on-disk')
        with patch.object(report, 'validate_success', mutate):
            result = report.load_run(self.raw)
        self.assertTrue(result['run_success'])
        self.assertEqual(result['raw_artifact_sha256'], {k: common.digest(v) for k, v in original.items()})
    def test_score_hash_matches_parsed_snapshot_during_file_mutation(self):
        _, result = self.execute()
        scores = report.score_template(result)
        scores['graded_at'] = common.now()
        for row in scores['rows']:
            row.update(score=5, reason='수동 테스트 예시', fabricated_personal_fact=False)
        path = self.root / 'scores.json'
        raw = common.canonical(scores)
        path.write_bytes(raw)
        original = report.validate_scores
        def mutate(run_data, score_data):
            rows = original(run_data, score_data)
            scores['rows'][0]['score'] = 1
            path.write_bytes(common.canonical(scores))
            return rows
        with patch.object(report, 'validate_scores', mutate):
            summary = report.prepare(self.raw, self.root / 'report', path)
        self.assertEqual(summary['overall_equal_category_mean'], 5)
        self.assertEqual(summary['scores_file_sha256'], common.digest(raw))
    def test_interrupted_trailing_line(self):
        self.execute()
        self.mutate_events(lambda es: es.__delitem__(slice(10, None)))
        with (self.raw / 'events.jsonl').open('ab') as f:
            f.write(b'{"incomplete')
        result = report.load_run(self.raw)
        self.assertTrue(result['journal_damaged'])
        self.assertEqual(result['counts']['baseline']['interrupted'], ['r1-N01'])
        self.assertFalse(result['schedule_complete'])
    def test_invalid_order_hash_and_duplicates(self):
        self.execute()
        original = (self.raw / 'events.jsonl').read_bytes()
        for mutation in (
            lambda es: es[10].update(attempt_id='r1-N02'),
            lambda es: es[10]['result'].update(text='altered'),
            lambda es: es[-1].update(failures=2),
            lambda es: es[9].update(round=3),
        ):
            (self.raw / 'events.jsonl').write_bytes(original)
            self.mutate_events(mutation)
            with self.assertRaises(Exception):
                report.load_run(self.raw)
    def test_write_failure_stops(self):
        original = run.Journal.append
        def fail(journal, kind, **fields):
            if kind == 'attempt_result':
                raise common.EvaluationError('WRITE', fatal=True)
            return original(journal, kind, **fields)
        with patch.object(run.Journal, 'append', fail):
            with self.assertRaises(common.EvaluationError):
                run.execute('unused', self.raw, Fake)
        result = report.load_run(self.raw)
        self.assertEqual(result['counts']['warmup']['interrupted'], ['w1'])

if __name__ == '__main__':
    unittest.main()
