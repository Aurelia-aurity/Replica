"""Generate one issue #36 suite with the original baseline GPU protocol."""
import argparse
import signal
import sys
from pathlib import Path
from assets import EvaluationError, baseline_module, inputs
from kobest import hash_file


def execute(suite, model_path, output, runtime_factory=None, profile=None):
    def provider():
        raw, data, release, config = inputs(suite, profile)
        if runtime_factory:
            config['model_artifacts_sha256'] = {'mock': 'synthetic_not_model_weights'}
        else:
            model = Path(model_path).resolve(strict=True)
            selected = [p for p in sorted(model.iterdir()) if p.is_file() and
                        (p.name.endswith('.safetensors') or p.name in
                         ('config.json', 'generation_config.json', 'tokenizer.json', 'tokenizer_config.json',
                          'special_tokens_map.json', 'model.safetensors.index.json', 'vocab.json', 'merges.txt'))]
            if not any(p.name.endswith('.safetensors') for p in selected):
                raise EvaluationError('MODEL')
            config['model_artifacts_sha256'] = {p.name: hash_file(p) for p in selected}
        return raw, data, release, config
    return baseline_module('run').execute(model_path, output, runtime_factory,
                                         input_provider=provider)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['human', 'record_qa', 'instructions'], required=True)
    parser.add_argument('--model-path', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', help='model-profiles.json entry (#37); omit for the #36 baseline')
    args = parser.parse_args()
    prior = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        return execute(args.suite, args.model_path, args.output, profile=args.profile)
    except (EvaluationError, OSError, KeyboardInterrupt):
        print('실행 중단: 출력 경로·환경·입력 계약을 확인하세요. 기존 결과는 새 실행으로 대체하지 마세요.', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, prior)


if __name__ == '__main__':
    raise SystemExit(main())
