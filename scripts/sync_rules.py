"""Pure, hold-free status rules for the issue #80 shadow engine."""
from __future__ import annotations

from typing import TypedDict

STATUSES = ("백로그", "준비 중", "진행 중", "검토 중", "완료", "취소")


class Facts(TypedDict):
    state: str
    reason: str | None
    ready: bool


class Decision(TypedDict):
    target: str | None
    write_project: bool
    write_notion: bool
    note: str


def decide(last_synced: str | None, notion_status: str | None,
           project_status: str | None, facts: Facts,
           last_facts: Facts | None) -> Decision:
    """Compare both sides to their baseline; semantic fact transitions run once.

    The five arguments are the public rule contract specified by issue #80.
    A cancellation already present in the baseline is not a new Notion request.
    """
    target = None
    if facts["state"] == "CLOSED":
        target = {"COMPLETED": "완료", "NOT_PLANNED": "취소", "DUPLICATE": "취소"}.get(facts["reason"])
        if target is None:
            return {"target": None, "write_project": False,
                    "write_notion": False, "note": "확인 필요"}
    elif last_facts is not None and facts != last_facts:
        if last_facts["state"] == "CLOSED":
            target = "검토 중" if facts["ready"] else "백로그"
        elif facts["ready"] and not last_facts["ready"]:
            target = "검토 중"
        elif last_facts["ready"] and not facts["ready"]:
            target = "진행 중"
    if target is not None:
        return {"target": target, "write_project": project_status != target,
                "write_notion": notion_status != target, "note": ""}

    invalid_notion = (notion_status not in STATUSES or
                      (notion_status == "취소" and notion_status != last_synced))
    if invalid_notion:
        target = last_synced if last_synced in STATUSES else project_status
        return {"target": target, "write_project": project_status is None and target is not None,
                "write_notion": target is not None and notion_status != target, "note": "확인 필요"}
    if last_synced is None:
        target = notion_status
        note = "초기 정렬" if notion_status != project_status else ""
    elif notion_status != last_synced:
        target, note = notion_status, ""
    else:
        target = project_status if project_status is not None else last_synced
        note = ""
    return {"target": target, "write_project": project_status != target,
            "write_notion": notion_status != target, "note": note}
