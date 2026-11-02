"""Send immediate GitHub payload events without a ledger or timeline polling."""
import os
import re

from discord_alerts import (Client, Error, PROJECT_URL, REPO, deliver, escape,
                            load_event, mode, payload, user_map)


def changed_reviewers(reviews, before: str) -> list[str]:
    """Latest substantive review per user controls the first-push notice."""
    if not re.fullmatch(r"[0-9a-f]{40}", before):
        raise Error("Invalid synchronize before SHA")
    latest = {}
    for review in sorted(reviews, key=lambda item: (item.get("submitted_at") or "", item["id"])):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["login"].lower()] = review
    return [review["user"]["login"] for review in latest.values()
            if review["state"] == "CHANGES_REQUESTED" and review["commit_id"] == before]


def event_message(event_name: str, event: dict, mapping: dict, api: Client) -> dict | None:
    pull = event_name == "pull_request_target"
    if event_name not in {"issues", "pull_request_target"}:
        raise Error("Unsupported event")
    obj = event["pull_request" if pull else "issue"]
    number = obj["number"]
    heading = f"Replica · {'PR' if pull else 'Issue'} #{number} · {escape(obj['title'])}"
    url = f"https://github.com/{REPO}/{'pull' if pull else 'issues'}/{number}"
    action = event["action"]
    recipients, teams = [], []
    if action == "opened":
        description = "PR 생성" if pull else f"Issue 생성\nProject: {PROJECT_URL}"
    elif action == "closed":
        description = ("PR 병합" if obj["merged"] else "병합 없이 PR 종료") if pull else {
            "completed": "Issue 종료 · 완료", "not_planned": "Issue 종료 · 미계획",
            "duplicate": "Issue 종료 · 중복"}.get(obj.get("state_reason"), "Issue 종료")
    elif action == "reopened":
        description = "재오픈"
    elif pull and action in {"review_requested", "ready_for_review", "synchronize"}:
        if obj["draft"]:
            return None
        if action == "review_requested":
            reviewer, team = event.get("requested_reviewer"), event.get("requested_team")
            if (reviewer is None) == (team is None):
                raise Error("Invalid review request identity")
            recipients = [reviewer["login"]] if reviewer else []
            teams = [team.get("name") or team["slug"]] if team else []
            description = "리뷰 요청 · 변경 사항을 확인하고 리뷰해주세요."
        elif action == "ready_for_review":
            recipients = [reviewer["login"] for reviewer in obj["requested_reviewers"]]
            teams = [team.get("name") or team["slug"] for team in obj["requested_teams"]]
            description = "리뷰 요청 · Ready로 전환됐습니다. 변경 사항을 확인하고 리뷰해주세요."
        else:
            recipients = changed_reviewers(
                api.pages(f"/repos/{REPO}/pulls/{number}/reviews"), event["before"])
            description = "수정 반영 · 변경 요청에 대한 새 커밋이 올라왔습니다. 재검토해주세요."
        if not recipients and not teams:
            return None
        if teams:
            description += " 팀: " + ", ".join(escape(team, 80) for team in teams)
    else:
        return None
    return payload(heading, description, url, mapping, recipients=recipients)


def main(env=None, *, client=None, send=deliver) -> int:
    env = os.environ if env is None else env
    try:
        if mode(env) == "off":
            print("Discord event alerts disabled (off)")
            return 0
        event = load_event(env)
        message = event_message(env["GITHUB_EVENT_NAME"], event,
                                user_map(env.get("DISCORD_USER_MAP", "{}")),
                                client or Client(env.get("GITHUB_TOKEN", "")))
        if message:
            send(message, env)
        else:
            print("No actionable event notification")
        return 0
    except Error as exc:
        print(f"Discord event alert failed: {exc}")
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Discord event alert failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
