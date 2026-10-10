"""Run the confirmed offline baseline; never retry or overwrite a run."""
import argparse
import json
import os
import signal
import sys
import uuid
from pathlib import Path
from common import (ROOT, EvaluationError, canonical, digest, inputs, now,
                    public_slot, slots, supplemental, validate_success, write_json)

class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise EvaluationError('ARGUMENT') from None

class Journal:
    def __init__(self, path, run_id):
        self.handle = path.open('x', encoding='utf-8')
        self.run_id, self.seq = run_id, 0
        self.usable, self.terminal_written = True, False
        self.results = {}

    def append(self, kind, **fields):
        if not self.usable or self.terminal_written:
            raise EvaluationError('WRITE', fatal=True)
        event = dict(type=kind, run_id=self.run_id, seq=self.seq, time=now(), **fields)
        # Any interrupted write may leave a partial final line. Latch the journal
        # unusable, never append a terminal behind it, and let the reader classify
        # the bytes that remain. This also works with library worker threads.
        try:
            self.handle.write(canonical(event).decode('utf-8') + '\n')
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.seq += 1
            if kind == 'attempt_result':
                self.results[fields['attempt_id']] = event
            if kind == 'run_end':
                self.terminal_written = True
        except OSError:
            self.usable = False
            raise EvaluationError('WRITE', fatal=True) from None
        except BaseException:
            self.usable = False
            raise

    def close(self):
        self.handle.close()

def execute(model_path, output, runtime_factory=None):
    raw, dataset, release, config = inputs()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    run_id = str(uuid.uuid4())
    write_json(output / 'metadata.json', {
        'schema_version': 1, 'run_id': run_id, 'created_at': now(),
        'execution_kind': 'mock' if runtime_factory else 'school_gpu_user_run',
        'dataset_sha256': digest(raw), 'rubric_sha256': release['rubric_sha256'],
        'config': config, 'config_sha256': digest(canonical(config)),
        'release': release, 'git_commit': None,
        'code_sha256': {p.name: digest(p.read_bytes()) for p in sorted(ROOT.glob('*.py'))},
    })
    with (output / 'cases.json').open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    journal = Journal(output / 'events.jsonl', run_id)
    failures, recovered, stopped = 0, False, False
    warm, baseline = slots(dataset, config)
    supplements = []
    baseline_done = supplement_done = False
    def completion():
        recorded = journal.results
        base_done = all(s['attempt_id'] in recorded for s in baseline)
        expected = ['s-' + s['attempt_id'] for s in baseline
                    if s['attempt_id'] in recorded and recorded[s['attempt_id']]['status'] == 'success'
                    and recorded[s['attempt_id']]['result']['termination'] == 'token_limit']
        supp_done = base_done and all(aid in recorded for aid in expected)
        warm_ok = all(s['attempt_id'] in recorded and recorded[s['attempt_id']]['status'] == 'success' for s in warm)
        fail_count = sum(e['status'] == 'failed' for e in recorded.values())
        return base_done, supp_done, warm_ok and base_done and supp_done, fail_count
    try:
        journal.append('run_start')
        journal.append('load_start')
        if runtime_factory is None:
            from runtime import Runtime
            runtime_factory = Runtime
        try:
            runtime = runtime_factory(model_path, config)
        except EvaluationError as exc:
            journal.append('load_result', status='failed', error_code=exc.code)
            stopped = True
        except Exception:
            journal.append('load_result', status='failed', error_code='LOAD')
            stopped = True
        else:
            journal.append('load_result', status='success', load_ms=runtime.load_ms,
                           environment=runtime.environment)
            def attempt(slot):
                nonlocal failures, recovered
                info = public_slot(slot)
                journal.append('attempt_start', **info, after_recovery=recovered)
                try:
                    result = runtime.generate(slot['prompt'], slot['cap'])
                    validate_success(result, slot['cap'])
                except EvaluationError as exc:
                    failures += 1
                    journal.append('attempt_result', **info, after_recovery=recovered,
                                   status='failed', error_code=exc.code,
                                   fatal=exc.fatal, recovery_changed_cache=exc.recovered)
                    recovered = recovered or exc.recovered
                    return None, exc.fatal
                except Exception:
                    failures += 1
                    journal.append('attempt_result', **info, after_recovery=recovered,
                                   status='failed', error_code='RUNTIME', fatal=True,
                                   recovery_changed_cache=False)
                    return None, True
                journal.append('attempt_result', **info, after_recovery=recovered,
                               status='success', result=result)
                return result, False
            for slot in warm:
                result, fatal = attempt(slot)
                if result is None:
                    stopped = True
                    break
            if not stopped:
                for slot in baseline:
                    result, fatal = attempt(slot)
                    if result and result['termination'] == 'token_limit':
                        supplements.append(supplemental(slot))
                    if fatal:
                        stopped = True
                        break
                baseline_done = not stopped
            if baseline_done:
                for slot in supplements:
                    _, fatal = attempt(slot)
                    if fatal:
                        stopped = True
                        break
                supplement_done = not stopped
        baseline_done, supplement_done, schedule_done, failures = completion()
        journal.append('run_end', status='aborted' if stopped else ('failed' if failures else 'success'),
                       baseline_complete=baseline_done, supplements_complete=supplement_done,
                       schedule_complete=schedule_done, failures=failures)
        return 0 if baseline_done and supplement_done and not failures else 1
    except KeyboardInterrupt:
        baseline_done, supplement_done, schedule_done, failures = completion()
        if journal.usable and not journal.terminal_written:
            try:
                journal.append('run_end', status='interrupted', schedule_complete=schedule_done,
                               baseline_complete=baseline_done, supplements_complete=supplement_done, failures=failures)
            except (KeyboardInterrupt, EvaluationError):
                pass  # best effort only; never append again after a damaged write
        return 1
    finally:
        journal.close()

def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--model-path', required=True)
    parser.add_argument('--output', type=Path, required=True)
    old = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        args = parser.parse_args(argv)
        return execute(args.model_path, args.output)
    except EvaluationError as exc:
        print(f'[{exc.code}] 평가 설정·실행 상태를 확인하세요.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('[INTERRUPTED] 실행이 중단됐습니다. 새 경로에서 다시 시작하세요.', file=sys.stderr)
        return 1
    except Exception:
        print('[ARTIFACT] 새 출력 경로와 파일 권한을 확인하세요.', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old)

if __name__ == '__main__':
    raise SystemExit(main())
