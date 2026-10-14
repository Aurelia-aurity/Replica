"""Optional pinned-harness integration check: synthetic likelihoods, CPU only."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kobest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    harness, data = args.harness.resolve(), args.data_dir.resolve()
    lock = kobest.verify_source(harness, data)
    sys.path.insert(0, str(harness))
    from lm_eval.api.model import LM
    from lm_eval.evaluator import simple_evaluate

    class SyntheticLM(LM):
        def loglikelihood(self, requests):
            return [(-1.0, False) for _ in requests]
        def loglikelihood_rolling(self, requests):
            raise AssertionError('KoBEST must use choice loglikelihood')
        def generate_until(self, requests):
            raise AssertionError('KoBEST must not generate')

    ts = kobest.tasks(harness, data, lock)
    result = simple_evaluate(model=SyntheticLM(), tasks=ts, num_fewshot=0,
              bootstrap_iters=0, log_samples=True, apply_chat_template=False,
              system_instruction=None, torch_random_seed=None)
    validated = kobest.validate_result(result, lock)
    print('SYNTHETIC CPU API integration PASS; no actual model/GPU quality or performance result.')
    print({name: row['computed'] for name, row in validated['tasks'].items()})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
