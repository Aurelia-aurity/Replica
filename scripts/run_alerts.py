"""Stateless workflow failure/recovery and interim Notion hold notifications."""
import io
import json
import os
import stat
import zipfile
from urllib.parse import quote

import notification_report
from discord_alerts import (Client, Error, REPO, deliver, escape, load_event, mode,
                            payload, user_map)

FAILURES = {"failure", "timed_out", "startup_failure"}
SYNC_PATHS = {
    ".github/workflows/notion-sync.yml": "GitHub → Notion",
    ".github/workflows/notion-sync-request.yml": "Notion ordinary request",
}
CI_PATHS = {".github/workflows/ci.yml": "CI",
            ".github/workflows/backend-tests.yml": "Backend tests"}
REPORT_LOOKBACK = 20


def workflow_name(run: dict) -> str:
    # API `name` can be a dynamic run-name (ordinary requests use a UUID).
    path = run.get("path", "").split("@", 1)[0]
    return SYNC_PATHS.get(path) or CI_PATHS.get(path) or run["name"]


def repository_identity(repo) -> bool:
    return (isinstance(repo, dict) and repo.get("id") == notification_report.REPO_ID and
            repo.get("full_name") == REPO)


def trusted_sync_producer(run: dict) -> bool:
    """Display names and report JSON cannot authenticate a privileged producer."""
    path = run.get("path", "").split("@", 1)[0]
    if (path not in SYNC_PATHS or not repository_identity(run.get("repository")) or
            not repository_identity(run.get("head_repository"))):
        return False
    event = run["event"]
    if path == ".github/workflows/notion-sync.yml":
        # This API metadata describes the PR head, not the base-code checkout.
        if event == "pull_request_target":
            return True
        return event in {"schedule", "issues", "workflow_dispatch"} and run["head_branch"] == "main"
    return event == "workflow_dispatch" and run["head_branch"] == "main"


def scope(run: dict, ci_names: list[str]) -> str | None:
    if not repository_identity(run.get("repository")):
        return None
    name = workflow_name(run)
    if name in SYNC_PATHS.values():
        return "sync" if trusted_sync_producer(run) else None
    if name not in ci_names:
        return None
    if (run["event"] == "push" and run["head_branch"] == "main" and
            repository_identity(run.get("head_repository"))):
        return "main"
    if run["event"] == "pull_request":
        return "pr"
    return None


def earlier_runs(api: Client, run: dict, selected: str):
    """List order is newest-created first; reruns of this ID are not predecessors."""
    path = f"/repos/{REPO}/actions/workflows/{run['workflow_id']}/runs?status=completed"
    if selected == "pr":
        path += "&branch=" + quote(run["head_branch"], safe="")
    for previous in api.pages(path, "workflow_runs"):
        if (previous["id"] == run["id"] or
                previous["run_number"] >= run["run_number"] or
                previous["status"] != "completed" or
                previous["workflow_id"] != run["workflow_id"] or
                scope(previous, [workflow_name(run)]) != selected):
            continue
        if selected == "pr" and previous["head_branch"] != run["head_branch"]:
            continue
        if previous["conclusion"] in {"cancelled", "skipped"}:
            continue
        yield previous


def report_holds(api: Client, run: dict) -> set[tuple[int, str]] | None:
    """Only read the exact run/attempt report member, never extract archive paths."""
    try:
        name = f"notion-notification-{run['id']}-{run['run_attempt']}"
        artifacts = [item for item in api.pages(
            f"/repos/{REPO}/actions/runs/{run['id']}/artifacts", "artifacts")
            if item["name"] == name and not item["expired"]]
        if len(artifacts) != 1:
            return None
        data = api.download(f"/repos/{REPO}/actions/artifacts/{artifacts[0]['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [item for item in archive.infolist()
                       if item.filename == notification_report.FILE_NAME]
            if (len(members) != 1 or members[0].is_dir() or
                    members[0].file_size > notification_report.MAX_BYTES or
                    members[0].flag_bits & 1 or
                    stat.S_IFMT(members[0].external_attr >> 16) not in
                    {0, stat.S_IFREG}):
                return None
            with archive.open(members[0]) as stream:
                raw = stream.read(notification_report.MAX_BYTES + 1)
            if len(raw) > notification_report.MAX_BYTES:
                return None
            report = notification_report.validate(
                json.loads(raw), run_id=run["id"], attempt=run["run_attempt"])
        # pull_request_target head_sha is the PR head, NOT the trusted checkout SHA.
        if report["dry_run"] or not report["scan_complete"]:
            return None
        return {(item["issue_number"], item["reason_code"]) for item in report["holds"]}
    except (OSError, ValueError, KeyError, TypeError,
            zipfile.BadZipFile, NotImplementedError) as exc:
        print(f"Notification report unavailable or invalid ({type(exc).__name__}); "
              "hold comparison skipped")
        return None


def hold_changes(current: set[tuple[int, str]], previous: set[tuple[int, str]]) -> list[str]:
    current_by_issue, previous_by_issue = {}, {}
    for holds, by_issue in ((current, current_by_issue), (previous, previous_by_issue)):
        for number, code in holds:
            by_issue.setdefault(number, set()).add(code)
    descriptions = []
    for number in sorted(current_by_issue.keys() | previous_by_issue.keys()):
        old = previous_by_issue.get(number, set())
        new = current_by_issue.get(number, set())
        if not old:
            descriptions.append(f"동기화 보류 발생 · #{number} ({', '.join(sorted(new))})")
        elif not new:
            descriptions.append(f"동기화 보류 해제 · #{number}")
        elif old != new:
            descriptions.append(
                f"동기화 보류 사유 변경 · #{number} "
                f"({', '.join(sorted(old))} -> {', '.join(sorted(new))})")
    return descriptions


def hold_notifications(api: Client, run: dict) -> list[str]:
    current = report_holds(api, run)
    if current is None:
        print("Current valid report missing; hold comparison skipped")
        return []
    for checked, candidate in enumerate(earlier_runs(api, run, "sync"), 1):
        if checked > REPORT_LOOKBACK:
            break
        if candidate["conclusion"] != "success":
            continue
        old = report_holds(api, candidate)
        if old is not None:
            return hold_changes(current, old)
    print("Previous valid report missing within lookback; hold comparison skipped")
    return []


def pr_authors(api: Client, run: dict) -> list[str]:
    pulls = run.get("pull_requests")
    if not pulls:
        pulls = [pull for pull in api.pages(
            f"/repos/{REPO}/commits/{run['head_sha']}/pulls")
            if pull["head"]["ref"] == run["head_branch"]]
    authors = []
    for pull in pulls:
        if pull.get("base", {}).get("ref") != "main":
            continue
        detail = api.get(f"/repos/{REPO}/pulls/{pull['number']}")
        authors.append(detail["user"]["login"])
    if not authors:
        raise Error("PR author could not be identified")
    return list(dict.fromkeys(authors))


def notify_run(run: dict, api: Client, env, *, send=deliver) -> None:
    ci_names = json.loads(env.get("DISCORD_CI_WORKFLOWS") or '["CI", "Backend tests"]')
    if not isinstance(ci_names, list) or any(not isinstance(n, str) for n in ci_names):
        raise Error("Invalid CI workflow names")
    selected = scope(run, ci_names)
    conclusion = run["conclusion"]
    if selected is None or conclusion not in FAILURES | {"success"}:
        print("Workflow outcome or scope ignored")
        return
    mapping = user_map(env.get("DISCORD_USER_MAP", "{}"))
    pm = env.get("PM_GITHUB_LOGIN") or "Just-Simple0"
    name = workflow_name(run)
    heading = f"Replica · {escape(name)}"
    url = f"https://github.com/{REPO}/actions/runs/{run['id']}"
    for attempt in range(run["run_attempt"] - 1, 0, -1):
        previous = api.get(f"/repos/{REPO}/actions/runs/{run['id']}/attempts/{attempt}")
        if (previous["status"] == "completed" and
                previous["conclusion"] not in {"cancelled", "skipped"} and
                previous["id"] == run["id"] and
                previous["workflow_id"] == run["workflow_id"] and
                scope(previous, ci_names) == selected):
            break
    else:
        previous = next(earlier_runs(api, run, selected), None)
    was_failure = previous is not None and previous["conclusion"] in FAILURES
    if conclusion in FAILURES and not was_failure:
        recipients = pr_authors(api, run) if selected == "pr" else [pm]
        send(payload(heading, "실행 오류 · " + escape(conclusion), url, mapping,
                     recipients=recipients), env, pm=selected == "sync")
    elif conclusion == "success" and was_failure and selected != "pr":
        send(payload(heading, "실행 오류 복구", url, mapping), env, pm=selected == "sync")
    if (selected != "sync" or name != "GitHub → Notion" or conclusion != "success"):
        return
    for description in hold_notifications(api, run):
        recipients = [] if description.startswith("동기화 보류 해제") else [pm]
        send(payload(heading, description, url, mapping, recipients=recipients),
             env, pm=True)


def main(env=None, *, client=None, send=deliver) -> int:
    env = os.environ if env is None else env
    try:
        if mode(env) == "off":
            print("Discord run alerts disabled (off)")
            return 0
        event = load_event(env)
        notify_run(event["workflow_run"], client or Client(env.get("GITHUB_TOKEN", "")),
                   env, send=send)
        return 0
    except Error as exc:
        print(f"Discord run alert failed: {exc}")
        return 1
    except Exception as exc:  # CLI boundary: raw exception details must not reach workflow logs.
        print(f"Discord run alert failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
