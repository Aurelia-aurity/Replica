"""Run the unfrozen matrix skeleton against its own HTTP doubles."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest


class LocalMeasurementTests(unittest.TestCase):
    def test_all_evidence_and_offered_concurrency_cells(self):
        path = Path(__file__).resolve().parents[2] / "experiments/warmup_timing/measure_local.py"
        spec = importlib.util.spec_from_file_location("measure_local", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(module.main(["--rounds", "1"]), 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 28)
        self.assertEqual(len({r["request_id"] for r in rows}), 28)
        for count in (0, 3, 6, 10):
            for concurrency in (1, 2, 4):
                cell = [r for r in rows if (r["evidence_count"], r["concurrency"]) ==
                        (count, concurrency)]
                self.assertEqual(len(cell), concurrency)
                self.assertEqual(sorted(r["participant"] for r in cell),
                                 list(range(concurrency)))
        for row in rows:
            self.assertFalse(row["protocol_frozen"])
            self.assertIn(row["status"], (200, 429))
            self.assertLessEqual(row["send_ns"], row["complete_ns"])
        self.assertNotIn("Synthetic answer", output.getvalue())


if __name__ == "__main__":
    unittest.main()
