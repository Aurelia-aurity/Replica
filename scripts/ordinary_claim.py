"""Canonical ordinary-run authorization type shared by CLI and engine."""

from dataclasses import dataclass

import github_project as gp
import sync_engine as se

WORKFLOW_PATH = ".github/workflows/notion-sync-request.yml"


@dataclass(frozen=True)
class OrdinaryClaim:
    request_id: str
    generation: int
    run_id: int
    sha: str
    actor_id: int

    def body(self):
        return {"request_id": self.request_id, "generation": self.generation,
                "run_id": self.run_id, "sha": self.sha, "attempt": 1}

    def validate(self, env):
        se.require(env.get("SYNC_ORDINARY_REQUEST") == "true" and
                   env.get("NOTION_RELAY_ENABLED") == "true" and
                   env.get("NOTION_SYNC_ENABLED") == "true" and
                   env.get("GITHUB_ACTIONS") == "true" and
                   env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and
                   env.get("GITHUB_REPOSITORY") == gp.REPOSITORY and
                   env.get("GITHUB_REPOSITORY_ID") == str(gp.REPOSITORY_ID) and
                   env.get("GITHUB_REF") == "refs/heads/main" and
                   env.get("GITHUB_RUN_ATTEMPT") == "1" and
                   env.get("GITHUB_SHA") == self.sha and
                   env.get("GITHUB_RUN_ID") == str(self.run_id) and
                   env.get("GITHUB_ACTOR_ID") == str(self.actor_id) and
                   env.get("GITHUB_DISPATCH_ACTOR_ID") == str(self.actor_id) and
                   env.get("GITHUB_ACTOR") == env.get("GITHUB_TRIGGERING_ACTOR") and
                   bool(env.get("GITHUB_ACTOR")) and
                   env.get("GITHUB_WORKFLOW_REF") ==
                   f"{gp.REPOSITORY}/{WORKFLOW_PATH}@refs/heads/main" and
                   env.get("RELAY_REQUEST_ID") == self.request_id and
                   env.get("RELAY_GENERATION") == str(self.generation),
                   "일반 실행 요청의 고정 실행 문맥이 일치하지 않습니다")
        se.require(not any(env.get(name) for name in
                   ("SYNC_RESOLVE_ISSUES", "SYNC_APPROVED_SHA", "SYNC_DIAGNOSE_DATE_READBACK")) and
                   env.get("SYNC_DRY_RUN") in (None, "false", ""),
                   "일반 실행 요청에는 PM 재개/진단 입력을 사용할 수 없습니다")
