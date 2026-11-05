"""Static checks for executable workflow controls, not human-readable prose."""
import re
import unittest
from pathlib import Path


class ShadowWorkflowTests(unittest.TestCase):
    def test_shadow_has_read_only_execution_and_pinned_checkout(self):
        # Given:
        path = Path(__file__).resolve().parents[2] / ".github/workflows/notion-sync-shadow.yml"
        workflow = path.read_text(encoding="utf-8")
        # When:
        runs = re.findall(r"^\s+run: (.+)$", workflow, re.MULTILINE)
        # Then:
        self.assertEqual(runs, ["python3 -B scripts/sync_simple.py --dry-run --report sync-simple-plan.json"])
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+\w[\w-]*: write$")
        self.assertNotIn("${{ github.event.", "\n".join(runs))
        self.assertIn("SYNC_SIMPLE_APPLY: 'false'", workflow)
        for gate in ("github.actor_id) == vars.PM_GITHUB_USER_ID",
                     "github.actor == github.triggering_actor",
                     "github.run_attempt) == '1'",
                     "inputs.approved_sha == github.sha"):
            self.assertIn(gate, workflow)
