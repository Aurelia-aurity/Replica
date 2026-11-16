"""Executable workflow gates remain independent of the selected sync engine."""
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/notion-sync.yml").read_text()
        self.ordinary = (ROOT / ".github/workflows/notion-sync-request.yml").read_text()

    def test_both_writers_share_non_cancelling_serial_group_and_trusted_checkout(self):
        for workflow in (self.workflow, self.ordinary):
            self.assertIn("group: replica-notion-sync", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
            self.assertIn("ref: ${{ github.sha }}", workflow)
            self.assertIn("persist-credentials: false", workflow)
            self.assertNotRegex(workflow, r"(?m)^\s+[\w-]+: write$")

    def test_manual_dispatch_requires_pm_sha_matching_actor_and_first_attempt(self):
        for gate in ("github.actor_id) == vars.PM_GITHUB_USER_ID",
                     "github.actor == github.triggering_actor",
                     "github.run_attempt) == '1'", "inputs.approved_sha == github.sha"):
            self.assertIn(gate, self.workflow)
        self.assertRegex(self.workflow, r"inputs\.dry_run &&\s+github.ref == 'refs/heads/feat/101-sync-engine-cutover'")

    def test_shell_ref_gate_executes_for_main_feature_and_tags(self):
        block = self.workflow.split('          case "$SYNC_DISPATCH_REF" in', 1)[1].split("          esac", 1)[0]
        script = 'case "$SYNC_DISPATCH_REF" in' + block + "esac"
        for ref, dry, expected in (
                ("refs/heads/main", "false", 0),
                ("refs/heads/feat/101-sync-engine-cutover", "true", 0),
                ("refs/heads/feat/101-sync-engine-cutover", "false", 1),
                ("refs/tags/main", "true", 1), ("refs/heads/other", "true", 1)):
            result = subprocess.run(["bash", "-c", script],
                                    env={"SYNC_DISPATCH_REF": ref, "SYNC_DISPATCH_DRY_RUN": dry},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected)

    def test_ordinary_dispatch_requires_bound_actor_ref_and_enabled_gates(self):
        for gate in ("github.ref == 'refs/heads/main'", "github.event_name == 'workflow_dispatch'",
                     "github.actor_id) == vars.NOTION_RELAY_ACTOR_ID",
                     "github.actor == github.triggering_actor", "github.run_attempt) == '1'",
                     "vars.NOTION_RELAY_ENABLED == 'true'", "vars.NOTION_SYNC_ENABLED == 'true'"):
            self.assertIn(gate, self.ordinary)
        self.assertIn("run: python3 -B scripts/ordinary_sync_request.py", self.ordinary)

    def test_pinned_uploads_preserve_run_alert_artifact_contract(self):
        for workflow in (self.workflow, self.ordinary):
            self.assertIn("notion-notification-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
            self.assertIn("notion-notification-report.json", workflow)
            self.assertIn("sync-simple-plan-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
            self.assertNotRegex(workflow, r"uses: [^\n]+@(main|master|v\d+)\s*$")
