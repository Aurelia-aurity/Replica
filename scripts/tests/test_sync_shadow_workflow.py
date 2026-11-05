"""Static checks for executable workflow controls, not human-readable prose."""
import re
import unittest
from pathlib import Path


class ShadowWorkflowTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/notion-sync-shadow.yml"
        self.workflow = path.read_text(encoding="utf-8")

    def test_execution_is_read_only_and_uses_literal_arguments(self):
        # Given / When:
        runs = re.findall(r"^\s+run: (.+)$", self.workflow, re.MULTILINE)
        # Then:
        self.assertEqual(runs, ["python3 -B scripts/sync_simple.py --dry-run --report sync-simple-plan.json"])
        self.assertNotIn("${{ github.event.", "\n".join(runs))

    def test_checkout_is_pinned_without_persisted_credentials(self):
        # Given / When / Then:
        self.assertIn("ref: ${{ github.sha }}", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)

    def test_token_permissions_are_read_only(self):
        # Given / When:
        permissions = self.workflow.split("permissions:\n", 1)[1].split("\n\n", 1)[0]
        # Then:
        self.assertEqual(dict(re.findall(r"^\s+([\w-]+): (\w+)$", permissions, re.MULTILINE)),
                         {"contents": "read", "issues": "read", "pull-requests": "read"})

    def test_dispatch_requires_pm_sha_and_first_attempt(self):
        # Given / When / Then:
        for gate in ("github.actor_id) == vars.PM_GITHUB_USER_ID",
                     "github.actor == github.triggering_actor",
                     "github.run_attempt) == '1'",
                     "inputs.approved_sha == github.sha"):
            self.assertIn(gate, self.workflow)

    def test_upstream_completion_requires_success(self):
        # Given / When / Then:
        self.assertRegex(self.workflow, r"github\.event_name == 'workflow_run' &&\s+"
                         r"github\.event\.workflow_run\.conclusion == 'success'")
