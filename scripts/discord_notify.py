"""Reconcile trusted GitHub facts into Discord notifications; no Discord bot."""
import argparse
import copy
import datetime
import json
import os
import re
import sys
from pathlib import Path

import discord_transport as dt
import observation_clock

REPO = dt.REPO
REPO_ID = dt.REPO_ID
SYNC_ID = 377351519
SYNC_PATH = ".github/workflows/notion-sync.yml"
NOTIFIER_PATH = ".github/workflows/discord-notify.yml"
PM = "Just-Simple0"
PM_ID = "97959897"
PROJECT_URL = "https://github.com/users/Just-Simple0/projects/4/views/1"
FAILURES = {"failure", "timed_out", "startup_failure", "action_required"}


def escape(text, limit=200):
    text = str(text)[:limit].replace("\r", " ").replace("\n", " ")
    text = text.replace("@", "＠").replace("<", "＜").replace(">", "＞")
    return re.sub(r"([\\`*_~|\[\]])", r"\\\1", text)


def user_map(raw):
    data = dt.json_data(raw)
    if not isinstance(data, dict) or len(data) > 100:
        raise dt.Error("Invalid Discord user mapping")
    normalized = {}
    for login, snowflake in data.items():
        if (not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) or
                not isinstance(snowflake, str) or not re.fullmatch(r"[0-9]{17,20}", snowflake) or
                login.lower() in normalized):
            raise dt.Error("Invalid Discord user mapping")
        normalized[login.lower()] = snowflake
    return normalized


def payload(label, description, url, mapping, *, recipients=()):
    if not (re.fullmatch(r"https://github\.com/Aurelia-aurity/Replica/(?:issues|pull)/[1-9][0-9]*", url) or
            re.fullmatch(r"https://github\.com/Aurelia-aurity/Replica/actions/runs/[1-9][0-9]*", url)):
        raise dt.Error("Invalid notification source link")
    mentions, names = [], []
    for login in dict.fromkeys(recipients):
        uid = mapping.get(login.lower())
        if uid:
            mentions.append(uid)
            names.append(f"<@{uid}>")
        else:
            names.append(escape(login, 80))
    content = f"{' '.join(names)}\n{label}\n{description}\n{url}".strip()
    if len(content) >= 1900:
        raise dt.Error("Notification content too large")
    return {"content": content, "allowed_mentions": {"parse": [], "users": list(dict.fromkeys(mentions)),
                                                     "replied_user": False}}


def new_state():
    return {"schema": 2, "repository_id": REPO_ID, "seen": [], "outbox": {},
            "incidents": {}, "holds": {}, "sync_cursor": None, "invalidated": []}


def _empty_episode(episode_id, *, run_id=None, attempt=None, observed_at=None,
                   uncertainty=None):
    timer = observed_at is not None
    return {"episode_id": episode_id,
            "first_observed_run_id": run_id, "first_observed_attempt": attempt,
            "first_observed_at": observed_at,
            "timer_origin_at": observed_at if timer else None,
            "timer_origin_run_id": run_id if timer else None,
            "timer_origin_uncertainty_seconds": uncertainty if timer else None,
            "last_valid_observed_at": observed_at,
            "last_observation_run_id": run_id, "last_observation_attempt": attempt,
            "delay_alert_key": None, "delay_cancelled": False}


def upgrade_state(state):
    """Upgrade the v1 ledger in place while retaining every delivery fence."""
    if state["schema"] == 1:
        state["schema"] = 2
        for number, target in state["holds"].items():
            for code, reason in target["reasons"].items():
                reason["episode"] = _empty_episode(f"legacy:{number}:{code}")
    return state


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def merge_fact(pull):
    if type(pull.get("merged")) is not bool or "merged_at" not in pull or pull.get("state") not in ("open", "closed"):
        raise dt.Error("Incomplete PR merge facts")
    at = pull["merged_at"]
    if not pull["merged"]:
        if at is not None:
            raise dt.Error("Inconsistent PR merge facts")
        return None
    if pull["state"] != "closed" or not isinstance(at, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", at):
        raise dt.Error("Invalid PR merge facts")
    try:
        datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise dt.Error("Invalid PR merge timestamp") from None
    return at


def invalidate(state, key):
    """Keep unknown delivery fences, but durably prohibit a later not_sent POST."""
    if key not in state["outbox"]:
        return
    state["invalidated"] = sorted(set(state["invalidated"]) | {key})
    if state["outbox"][key]["status"] == "ready":
        state["outbox"][key]["status"] = "retired"


def end_hold_episode(state, reason):
    """Stop timers and unsent notices while retaining any delivery fence."""
    episode = reason.get("episode") or {}
    delay_key = episode.get("delay_alert_key")
    if delay_key and delay_key != "cancelled":
        invalidate(state, delay_key)
    alert = reason.get("alert")
    if alert:
        invalidate(state, alert)
    reason["streak"] = 0
    reason["episode"] = None


def retain_recovery_anchor(state, target, alert):
    """Remember delivered or ambiguous hold notices until issue-level recovery."""
    if (alert and alert.startswith(("hold:", "hold-delay:")) and
            state["outbox"].get(alert, {}).get("status") in ("delivered", "pending")):
        anchors = target.setdefault("recovery_alerts", [])
        if alert not in anchors:
            anchors.append(alert)


def retain_episode_recovery_anchors(state, target, reason):
    retain_recovery_anchor(state, target, reason.get("alert"))
    episode = reason.get("episode") or {}
    retain_recovery_anchor(state, target, episode.get("delay_alert_key"))


def seal_message(key, message):
    sealed = copy.deepcopy(message)
    sealed["content"] += "\n알림 ID: " + dt.digest(key)
    if len(sealed["content"]) >= 1900:
        raise dt.Error("Notification content too large")
    return sealed


def queue(state, key, message, *, dependencies=()):
    sealed = seal_message(key, message)
    state["outbox"].setdefault(key, {"payload": sealed, "hash": dt.digest(sealed),
                                   "status": "ready", "message_id": None,
                                   "dependencies": list(dependencies)})


def request_identity(event):
    reviewer, team = event.get("requested_reviewer"), event.get("requested_team")
    if (reviewer is None) == (team is None):
        raise dt.Error("Invalid review request identity")
    if reviewer is not None:
        if not isinstance(reviewer, dict) or not isinstance(reviewer.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", reviewer["login"]):
            raise dt.Error("Invalid review request user")
        return "user", reviewer["login"]
    if not isinstance(team, dict) or not isinstance(team.get("slug"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", team["slug"]):
        raise dt.Error("Invalid review request team")
    return "team", team["slug"]


def current_reviewers(pull):
    result = []
    for key, attribute, pattern in (("requested_reviewers", "login", r"[A-Za-z0-9-]{1,39}"),
                                    ("requested_teams", "slug", r"[A-Za-z0-9_-]{1,100}")):
        if key not in pull or not isinstance(pull[key], list):
            raise dt.Error("Current review request list missing")
        names = set()
        for item in pull[key]:
            if (not isinstance(item, dict) or not isinstance(item.get(attribute), str) or
                    not re.fullmatch(pattern, item[attribute]) or item[attribute].casefold() in names):
                raise dt.Error("Invalid current review request identity")
            names.add(item[attribute].casefold())
        result.append(names)
    return result


def object_url(number, pull=False):
    if type(number) is not int or number <= 0:
        raise dt.Error("Invalid source number")
    return f"https://github.com/{REPO}/{'pull' if pull else 'issues'}/{number}"


def source_events(objects, timelines, mapping, backlog=()):
    """Return every semantic event, including suppressed requests for baseline IDs."""
    result = {}
    for obj in objects:
        pull = "head" in obj
        number = obj["number"]
        prefix = f"{'pr' if pull else 'issue'}:{number}"
        url = object_url(number, pull)
        heading = f"Replica · {'PR' if pull else 'Issue'} #{number} · {escape(obj['title'])}"
        created = "PR 생성" if pull else "Issue 생성"
        if not pull:
            created += (" · Project 백로그 등록 확인" if number in backlog else
                        " · Project 등록 미확인\nProject: " + PROJECT_URL)
        result[prefix + ":created:" + obj["created_at"]] = payload(heading, created, url, mapping)
        timeline = timelines[number]
        events = [x for x in timeline if x.get("event") in
                  {"closed", "reopened", "merged", "review_requested", "review_request_removed"}]
        events.sort(key=lambda x: (x["created_at"], x["id"]))
        latest_request = {}
        for event in events:
            if event["event"] in ("review_requested", "review_request_removed"):
                kind, name = request_identity(event)
                latest_request[(kind, name.casefold())] = event
        current_users, current_teams = current_reviewers(obj) if pull else (set(), set())
        merged_at = merge_fact(obj) if pull else None
        for event in events:
            action = event["event"]
            if type(event.get("id")) is not int or event["id"] <= 0:
                raise dt.Error("Invalid timeline identity")
            key = prefix + ":timeline:" + str(event["id"])
            if action == "merged" or pull and action == "closed" and merged_at and event["created_at"] >= merged_at:
                # Canonical REST merge below; keep timeline IDs consumed but silent.
                result[key] = None
            elif action in ("closed", "reopened"):
                label = ("병합 없이 PR 종료" if pull else "Issue 종료") if action == "closed" else "재오픈"
                result[key] = payload(heading, label, url, mapping)
            elif action == "review_requested" and pull:
                identity = request_identity(event)
                normalized = identity[0], identity[1].casefold()
                # A stale requested_reviewers list can survive PR closure. Review
                # requests are actionable only while the current PR is open.
                active = (obj.get("state") == "open" and
                          normalized[1] in (current_users if identity[0] == "user" else current_teams))
                if active and latest_request.get(normalized) == event:
                    names = [identity[1]] if identity[0] == "user" else []
                    description = "리뷰 요청 · 변경 사항을 확인하고 리뷰해주세요."
                    if identity[0] == "team":
                        description += " 팀: " + escape(identity[1], 80)
                    result[key] = payload(heading, description, url, mapping, recipients=names)
                else:
                    result[key] = None
            else:
                result[key] = None
        if merged_at:
            result[prefix + ":merged:" + merged_at] = payload(heading, "PR 병합", url, mapping)
    return result


def reconcile_events(state, events, *, bootstrap=False):
    seen = set(state["seen"])
    if not bootstrap:
        for key, message in events.items():
            if message is None and state["outbox"].get(key, {}).get("status") == "ready":
                # A crash before POST can leave a queued review request that was later cancelled.
                state["outbox"][key]["status"] = "retired"
            if key not in seen and message is not None:
                queue(state, key, message)
    state["seen"] = sorted(seen | events.keys())


def run_order(run):
    values = tuple(run[k] for k in ("run_number", "id", "run_attempt"))
    if any(type(x) is not int or x <= 0 for x in values):
        raise dt.Error("Invalid workflow run order")
    return values


def sync_run_binding(run):
    """Bind list and detail evidence used by sync notice decisions."""
    if not isinstance(run, dict):
        return None
    try:
        run_number, run_id, attempt = run_order(run)
    except (KeyError, TypeError, dt.Error):
        return None
    repository, head_repository = run.get("repository"), run.get("head_repository")
    repo_id = repository.get("id") if isinstance(repository, dict) else None
    head_repo_id = head_repository.get("id") if isinstance(head_repository, dict) else None
    workflow_id, path = run.get("workflow_id"), run.get("path")
    head_sha, status, conclusion = run.get("head_sha"), run.get("status"), run.get("conclusion")
    branch, event = run.get("head_branch"), run.get("event")
    if (type(workflow_id) is not int or workflow_id <= 0 or not isinstance(path, str) or not path or
            type(repo_id) is not int or repo_id <= 0 or type(head_repo_id) is not int or head_repo_id <= 0 or
            not valid_sha(head_sha) or not isinstance(status, str) or
            conclusion is not None and not isinstance(conclusion, str) or
            not isinstance(branch, str) or not branch or not isinstance(event, str) or not event):
        return None
    actor = run.get("actor") if isinstance(run.get("actor"), dict) else {}
    triggering_actor = (run.get("triggering_actor")
                        if isinstance(run.get("triggering_actor"), dict) else {})
    actor_id = actor.get("id") if event == "workflow_dispatch" else None
    triggering_actor_id = triggering_actor.get("id") if event == "workflow_dispatch" else None
    return (run_id, run_number, attempt, workflow_id, path, repo_id, head_repo_id,
            head_sha, status, conclusion, branch, event, actor_id, triggering_actor_id)


def verified_sync_run_detail(gh, listed):
    binding = sync_run_binding(listed)
    if binding is None:
        return None
    try:
        detail = gh.repo(f"/actions/runs/{listed['id']}")
    except Exception:
        return None
    return detail if sync_run_binding(detail) == binding else None


def sync_counter_bound(run):
    repository = run.get("repository") if isinstance(run, dict) else None
    head_repository = run.get("head_repository") if isinstance(run, dict) else None
    return (isinstance(run, dict) and type(run.get("workflow_id")) is int and
            run["workflow_id"] == SYNC_ID and run.get("path") == SYNC_PATH and
            isinstance(repository, dict) and type(repository.get("id")) is int and
            repository["id"] == REPO_ID and isinstance(head_repository, dict) and
            type(head_repository.get("id")) is int and head_repository["id"] == REPO_ID)


def sync_run_status(run):
    status = run.get("status") if isinstance(run, dict) else None
    return status if isinstance(status, str) and status in {
        "queued", "in_progress", "completed", "waiting", "requested", "pending"
    } else None


def reset_sync_streaks(state):
    for target in state["holds"].values():
        for reason in target["reasons"].values():
            alert = reason.get("alert")
            status = state["outbox"].get(alert, {}).get("status") if alert else None
            if status in ("ready", "pending", "delivered"):
                # A previously confirmed alert remains a valid notice even
                # when a newer run is pending or an interval becomes unknown.
                continue
            retain_episode_recovery_anchors(state, target, reason)
            end_hold_episode(state, reason)


def failure_episode(state, stream, failed, run, mapping, *, owner, label, remaining=""):
    incident = state["incidents"].get(stream)
    url = f"https://github.com/{REPO}/actions/runs/{run['id']}"
    if failed:
        if incident is None:
            for old in state["outbox"]:
                if old.startswith(f"recovery:failure:{stream}:"):
                    invalidate(state, old)
            key = f"failure:{stream}:{run['id']}:{run['run_attempt']}"
            queue(state, key, payload("Replica · " + label, "실패 · 실행 로그를 확인하고 조치해주세요.",
                                      url, mapping, recipients=[owner]))
            state["incidents"][stream] = {"alert": key}
    elif incident:
        invalidate(state, incident["alert"])
        key = "recovery:" + incident["alert"] + f":at:{run['id']}:{run['run_attempt']}"
        queue(state, key, payload("Replica · " + label,
                                  "실행 오류 복구" + remaining, url, mapping),
              dependencies=[incident["alert"]])
        del state["incidents"][stream]


def ci_verdict(run, pull, workflow, main_sha, *, historical=False):
    """True: proven association; False: proven mismatch; None: incomplete evidence."""
    if not isinstance(run, dict):
        return None
    # Exclude provably unrelated streams before considering missing evidence.
    if (type(run.get("workflow_id")) is int and run["workflow_id"] != workflow["id"] or
            isinstance(run.get("path"), str) and run["path"] != workflow["path"] or
            type((run.get("repository") or {}).get("id")) is int and run["repository"]["id"] != REPO_ID or
            isinstance(run.get("event"), str) and run["event"] != ("push" if pull is None else "pull_request") or
            pull is None and isinstance(run.get("head_branch"), str) and run["head_branch"] != "main" or
            pull is None and type((run.get("head_repository") or {}).get("id")) is int and run["head_repository"]["id"] != REPO_ID):
        return False
    associations = run.get("pull_requests")
    if pull is not None and isinstance(associations, list) and all(
            isinstance(a, dict) and type(a.get("number")) is int for a in associations):
        if not any(a["number"] == pull.get("number") for a in associations):
            return False
    if not valid_sha(run.get("head_sha")) or not valid_sha(main_sha):
        return None
    for field in ("workflow_id", "path", "event", "head_branch"):
        if field not in run or run[field] is None:
            return None
    for field in ("repository", "head_repository"):
        if type((run.get(field) or {}).get("id")) is not int:
            return None
    if (run["workflow_id"] != workflow["id"] or run["path"] != workflow["path"] or
            run["repository"]["id"] != REPO_ID):
        return False
    if pull is None:
        return (run["event"] == "push" and run["head_branch"] == "main" and
                (historical or run["head_sha"] == main_sha) and run["head_repository"]["id"] == REPO_ID)
    if pull.get("state") not in ("open", "closed"):
        return None
    if pull["state"] == "closed" or run["event"] != "pull_request":
        return False
    head, base = pull.get("head") or {}, pull.get("base") or {}
    if (not valid_sha(head.get("sha")) or type((head.get("repo") or {}).get("id")) is not int or
            type((base.get("repo") or {}).get("id")) is not int or not isinstance(base.get("ref"), str)):
        return None
    if base["ref"] != "main" or base["repo"]["id"] != REPO_ID:
        return False
    matching = {head["sha"]}
    if valid_sha(pull.get("merge_commit_sha")):
        matching.add(pull["merge_commit_sha"])
    if not historical and run["head_sha"] not in matching:
        return False
    associations = run.get("pull_requests")
    if not isinstance(associations, list):
        return None
    incomplete = False
    for association in associations:
        if not isinstance(association, dict):
            incomplete = True
            continue
        ah, ab = association.get("head") or {}, association.get("base") or {}
        if (type(association.get("number")) is not int or not valid_sha(ah.get("sha")) or
                type((ah.get("repo") or {}).get("id")) is not int or
                type((ab.get("repo") or {}).get("id")) is not int or not isinstance(ab.get("ref"), str)):
            incomplete = True
            continue
        if (association["number"] == pull["number"] and ah["repo"]["id"] == head["repo"]["id"] and
                (historical or ah["sha"] == head["sha"]) and ab["repo"]["id"] == REPO_ID and ab["ref"] == "main"):
            return True
    return None if incomplete else False


def ci_run_binding(run):
    """Normalize authorization-relevant CI identity without binding display metadata."""
    if not isinstance(run, dict):
        return None
    try:
        order = run_order(run)
    except (KeyError, TypeError, dt.Error):
        return None
    repository, head_repository = run.get("repository"), run.get("head_repository")
    repo_id = repository.get("id") if isinstance(repository, dict) else None
    head_repo_id = head_repository.get("id") if isinstance(head_repository, dict) else None
    workflow_id, path = run.get("workflow_id"), run.get("path")
    head_sha, status, conclusion = run.get("head_sha"), run.get("status"), run.get("conclusion")
    branch, event = run.get("head_branch"), run.get("event")
    if (type(workflow_id) is not int or workflow_id <= 0 or not isinstance(path, str) or not path or
            type(repo_id) is not int or repo_id <= 0 or type(head_repo_id) is not int or head_repo_id <= 0 or
            not valid_sha(head_sha) or not isinstance(status, str) or
            conclusion is not None and not isinstance(conclusion, str) or
            not isinstance(branch, str) or not branch or not isinstance(event, str) or not event):
        return None
    associations = None
    if event == "pull_request" and "pull_requests" in run:
        values = run["pull_requests"]
        if not isinstance(values, list):
            return None
        normalized = []
        for association in values:
            if not isinstance(association, dict):
                return None
            head, base = association.get("head") or {}, association.get("base") or {}
            head_repo = head.get("repo") or {}
            base_repo = base.get("repo") or {}
            number, sha, head_id = association.get("number"), head.get("sha"), head_repo.get("id")
            base_id, base_ref = base_repo.get("id"), base.get("ref")
            if (type(number) is not int or number <= 0 or not valid_sha(sha) or
                    type(head_id) is not int or head_id <= 0 or
                    type(base_id) is not int or base_id <= 0 or
                    not isinstance(base_ref, str) or not base_ref):
                return None
            normalized.append((number, sha, head_id, base_id, base_ref))
        associations = tuple(sorted(normalized))
    return (order, workflow_id, path, repo_id, head_repo_id, head_sha, status,
            conclusion, branch, event, associations)


def ci_matches(run, pull, workflow, main_sha):
    return ci_verdict(run, pull, workflow, main_sha) is True


def ci_counter_bound(run, workflow):
    repository = run.get("repository") if isinstance(run, dict) else None
    return (isinstance(run, dict) and type(run.get("workflow_id")) is int and
            run["workflow_id"] == workflow["id"] and
            run.get("path") == workflow["path"] and isinstance(repository, dict) and
            type(repository.get("id")) is int and repository["id"] == REPO_ID)


def _excluded_counter_tail_verified(gh, runs, latest_order, *, workflow_id,
                                    classify, verify_detail):
    """Prove excluded same-counter rows before accepting a provisional latest."""
    try:
        for listed in runs:
            repository = listed.get("repository") if isinstance(listed, dict) else None
            if (not isinstance(listed, dict) or type(listed.get("workflow_id")) is not int or
                    listed["workflow_id"] != workflow_id or not isinstance(repository, dict) or
                    type(repository.get("id")) is not int or repository["id"] != REPO_ID):
                continue
            if (classify(listed) is not False or
                    run_order(listed) <= latest_order):
                continue
            detail = verify_detail(gh, listed)
            if detail is None or classify(detail) is not False:
                return False
        return True
    except Exception:
        return False


def _verified_ci_run_detail(gh, listed):
    binding = ci_run_binding(listed)
    if binding is None:
        return None
    try:
        detail = gh.repo(f"/actions/runs/{listed['id']}")
    except Exception:
        return None
    return detail if ci_run_binding(detail) == binding else None


def _ci_counter_tail_verified(gh, runs, latest_order, pull, workflow, main_sha):
    return _excluded_counter_tail_verified(
        gh, runs, latest_order,
        workflow_id=workflow["id"],
        classify=lambda row: ci_verdict(row, pull, workflow, main_sha),
        verify_detail=_verified_ci_run_detail)


def _sync_counter_tail_verified(gh, runs, latest_order):
    return _excluded_counter_tail_verified(
        gh, runs, latest_order,
        workflow_id=SYNC_ID,
        classify=lambda row: sync_scope(row, workflow_endpoint=True),
        verify_detail=verified_sync_run_detail)


def ci_recovery_counter_rows(observed, workflow, boundary_order, latest_order):
    """Require a gap-free workflow counter interval without borrowing foreign rows."""
    rows_by_number, numbers_by_id = {}, {}
    for run in observed:
        if not isinstance(run, dict):
            return None
        workflow_id, path = run.get("workflow_id"), run.get("path")
        repository = run.get("repository")
        repository_id = repository.get("id") if isinstance(repository, dict) else None
        if ((type(workflow_id) is int and workflow_id != workflow["id"]) or
                (isinstance(path, str) and path != workflow["path"]) or
                (type(repository_id) is int and repository_id != REPO_ID)):
            continue
        try:
            order = run_order(run)
        except (KeyError, TypeError, dt.Error):
            return None
        if order[0] < boundary_order[0] or order[0] > latest_order[0]:
            continue
        if not ci_counter_bound(run, workflow):
            return None
        run_number, run_id, attempt = order
        previous_number = numbers_by_id.get(run_id)
        if previous_number is not None and previous_number != run_number:
            return None
        numbers_by_id[run_id] = run_number
        attempts = rows_by_number.setdefault(run_number, {})
        existing_id = next(iter(attempts.values()))[0] if attempts else None
        if existing_id is not None and existing_id != run_id:
            return None
        if attempt in attempts:
            prior = attempts[attempt][1]
            fields = ("workflow_id", "path", "repository", "head_repository", "head_sha",
                      "status", "conclusion", "event", "pull_requests")
            if any(prior.get(field) != run.get(field) for field in fields):
                return None
            continue
        attempts[attempt] = (run_id, run)
    if any(number not in rows_by_number
           for number in range(boundary_order[0], latest_order[0] + 1)):
        return None
    return rows_by_number


def reconcile_ci(state, runs, pulls, workflows, main_sha, mapping):
    for workflow in workflows:
        for pull in [None] + pulls:
            stream = f"ci:{workflow['id']}:" + (f"pr:{pull['number']}" if pull else "main")
            if pull and pull.get("state") not in ("open", "closed"):
                continue
            if pull and pull["state"] == "closed":
                incident = state["incidents"].pop(stream, None)
                if incident:
                    invalidate(state, incident["alert"])
                for key, item in state["outbox"].items():
                    if key.startswith((f"failure:{stream}:", f"recovery:failure:{stream}:")):
                        invalidate(state, key)
                continue
            eligible = [r for r in runs if ci_matches(r, pull, workflow, main_sha)]
            if not eligible:
                continue
            latest = max(eligible, key=run_order)
            if any(ci_verdict(r, pull, workflow, main_sha) is None and run_order(r) >= run_order(latest)
                   for r in runs):
                continue
            if latest["status"] != "completed":
                continue
            conclusion = latest.get("conclusion")
            if conclusion in FAILURES:
                incident = state["incidents"].get(stream)
                if incident:
                    alert = incident["alert"]
                    item = state["outbox"].get(alert, {})
                    original_parts = alert.split(":")
                    old_id, old_attempt = int(original_parts[-2]), int(original_parts[-1])
                    original = next((r for r in runs if r["id"] == old_id), None)
                    obsolete = item.get("status") == "retired" or (original is not None and
                        (ci_verdict(original, pull, workflow, main_sha) is False or
                         original.get("run_attempt") != old_attempt))
                    if item.get("status") in ("ready", "retired") and obsolete:
                        invalidate(state, alert)
                        del state["incidents"][stream]
            if conclusion in FAILURES or conclusion == "success":
                failure_episode(state, stream, conclusion in FAILURES, latest, mapping,
                                owner=pull["user"]["login"] if pull else PM,
                                label=f"CI · {escape(workflow['name'], 100)}" +
                                      (f" · PR #{pull['number']}" if pull else " · main"))


def reconcile_sync(state, run, report, mapping, *, current_snapshot=True):
    """Consume all distinct runs, with unknowns interrupting unannounced streaks."""
    order = run_order(run)
    prior = tuple(state["sync_cursor"]) if state["sync_cursor"] else None
    if prior and order <= prior:
        return
    same_run = prior and order[:2] == prior[:2]
    state["sync_cursor"] = list(order)
    full = bool(run.get("conclusion") == "success" and report and report["scan_complete"] and not report["dry_run"] and
                report["kind"] in ("complete", "partial"))
    failed = run.get("conclusion") in FAILURES
    if failed:
        failure_episode(state, "sync", True, run, mapping, owner=PM, label="Notion 동기화")
    if not full:
        for target in state["holds"].values():
            for reason in target["reasons"].values():
                reason["streak"] = 0
        return
    held = {}
    for item in report["holds"]:
        held.setdefault(str(item["issue_number"]), set()).add(item["reason_code"])
    if current_snapshot:
        failure_episode(state, "sync", False, run, mapping, owner=PM, label="Notion 동기화",
                        remaining=f" · 보류 {len(held)}건 남음" if held else " · 보류 없음")
    for number in list(state["holds"]):
        if number not in held:
            if not current_snapshot:
                target = state["holds"][number]
                for reason in target["reasons"].values():
                    retain_episode_recovery_anchors(state, target, reason)
                    end_hold_episode(state, reason)
                continue
            target = state["holds"].pop(number)
            for reason in target["reasons"].values():
                retain_episode_recovery_anchors(state, target, reason)
                episode = reason.get("episode") or {}
                delay_key = episode.get("delay_alert_key")
                if delay_key and delay_key != "cancelled":
                    invalidate(state, delay_key)
            alerts = list(dict.fromkeys(
                target.get("recovery_alerts", []) +
                [r["alert"] for r in target["reasons"].values() if r.get("alert")]))
            alerts = [alert for alert in alerts
                      if state["outbox"].get(alert, {}).get("status") in ("delivered", "pending")]
            if alerts:
                for alert in alerts:
                    invalidate(state, alert)
                queue(state, f"hold-recovery:{number}:{run['id']}:{run['run_attempt']}",
                      payload(f"Replica · Issue #{number}",
                              f"동기화 보류 해제 · 남은 보류 {len(held)}건",
                              object_url(int(number)), mapping), dependencies=alerts)
    for number, codes in held.items():
        for key in state["outbox"]:
            if key.startswith(f"hold-recovery:{number}:"):
                invalidate(state, key)
        target = state["holds"].setdefault(number, {"reasons": {}})
        for code, reason in target["reasons"].items():
            if code not in codes:
                if current_snapshot:
                    retain_episode_recovery_anchors(state, target, reason)
                    if reason["alert"]:
                        invalidate(state, reason["alert"])
                    episode = reason.get("episode") or {}
                    delay_key = episode.get("delay_alert_key")
                    if delay_key and delay_key != "cancelled":
                        invalidate(state, delay_key)
                    # The latest complete/partial snapshot ended this reason's
                    # episode. Keep old outbox records as fences; recurrence
                    # starts with a clean alert reference and a fresh streak.
                    reason.update(streak=0, alert=None, episode=None)
                else:
                    retain_episode_recovery_anchors(state, target, reason)
                    end_hold_episode(state, reason)
        for code in sorted(codes):
            reason = target["reasons"].setdefault(
                code, {"streak": 0, "alert": None, "episode": None})
            if reason["streak"] == 0 and reason["episode"] is None:
                # A code returning after an observed clear starts a clean
                # episode; an old pending outbox entry remains fenced by key.
                reason["alert"] = None
            # One workflow run is one observation, regardless of attempt count.
            if not same_run:
                reason["streak"] = min(2, reason["streak"] + 1)
            elif reason["streak"] == 0:
                reason["streak"] = 1
            episode = reason.get("episode")
            observed_at = report.get("observed_at") if report.get("schema") == 2 else None
            uncertainty = (report.get("observation_uncertainty_seconds")
                           if observed_at is not None else None)
            if episode is None:
                episode = _empty_episode(
                    f"{number}:{code}:{run['id']}:{run['run_attempt']}",
                    run_id=run["id"], attempt=run["run_attempt"],
                    observed_at=observed_at, uncertainty=uncertainty)
                reason["episode"] = episode
            else:
                origin_run = episode.get("timer_origin_run_id")
                if not same_run and origin_run is not None and run["id"] != origin_run:
                    episode["delay_cancelled"] = True
                    delay_key = episode.get("delay_alert_key")
                    if delay_key and delay_key != "cancelled":
                        retain_recovery_anchor(state, target, delay_key)
                        invalidate(state, delay_key)
                    episode["delay_alert_key"] = "cancelled"
                # A legacy/clock-unknown episode starts a new timer only from a
                # later v2 observation; its original run identity remains unknown.
                if episode.get("timer_origin_at") is None and observed_at is not None:
                    episode["timer_origin_at"] = observed_at
                    episode["timer_origin_run_id"] = run["id"]
                    episode["timer_origin_uncertainty_seconds"] = uncertainty
                    episode["delay_cancelled"] = False
                if not same_run:
                    episode["last_valid_observed_at"] = observed_at
                    episode["last_observation_run_id"] = run["id"]
                    episode["last_observation_attempt"] = run["run_attempt"]
            if reason["streak"] == 2 and not reason["alert"]:
                key = f"hold:{number}:{code}:{run['id']}"
                queue(state, key, payload(f"Replica · Issue #{number}",
                                          f"동기화 보류 · {escape(code, 80)}\n두 실행에서 계속 보류됐습니다. 원인을 확인해주세요.",
                                          object_url(int(number)), mapping, recipients=[PM]))
                reason["alert"] = key


def reconcile_hold_delays(state, clock_reading, mapping):
    """Queue one PM notice only when both endpoint clock bounds are verified."""
    if not clock_reading or clock_reading[0] is None or clock_reading[1] is None:
        return
    try:
        now = datetime.datetime.fromisoformat(clock_reading[0].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return
    consumer_uncertainty = clock_reading[1]
    if type(consumer_uncertainty) is not int or not 0 <= consumer_uncertainty <= 60:
        return
    for number, target in state["holds"].items():
        for code, reason in target["reasons"].items():
            episode = reason.get("episode")
            if (not episode or episode.get("delay_cancelled") or
                    episode.get("delay_alert_key") is not None or
                    episode.get("timer_origin_at") is None or
                    episode.get("last_observation_run_id") != episode.get("timer_origin_run_id")):
                continue
            try:
                origin = datetime.datetime.fromisoformat(
                    episode["timer_origin_at"].replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            # The design fixes the combined conservative deduction at 120s
            # after independently validating each endpoint's 0..60s bound.
            elapsed = (now - origin).total_seconds() - 120
            if elapsed < 15 * 60:
                continue
            key = f"hold-delay:{number}:{code}:{episode['episode_id']}"
            queue(state, key, payload(f"Replica · Issue #{number}",
                                      f"동기화 보류 후속 관측 지연 · {escape(code, 80)}\n"
                                      "15분 동안 후속 유효 실행을 확인하지 못했습니다. 확인해주세요.",
                                      object_url(int(number)), mapping, recipients=[PM]))
            episode["delay_alert_key"] = key


def notice_active(state, key):
    if key in state["invalidated"]:
        return False
    if key.startswith("failure:"):
        stream = key[len("failure:"):].rsplit(":", 2)[0]
        return state["incidents"].get(stream, {}).get("alert") == key
    if key.startswith("recovery:failure:"):
        stream = key.split(":at:")[0][len("recovery:failure:"):].rsplit(":", 2)[0]
        return stream not in state["incidents"]
    if key.startswith("hold:"):
        _, number, code, _ = key.split(":")
        reason = state["holds"].get(number, {}).get("reasons", {}).get(code, {})
        return reason.get("alert") == key and reason.get("episode") is not None
    if key.startswith("hold-delay:"):
        parts = key.split(":", 3)
        if len(parts) != 4:
            return False
        reason = state["holds"].get(parts[1], {}).get("reasons", {}).get(parts[2], {})
        episode = reason.get("episode") or {}
        return (episode.get("delay_alert_key") == key and
                not episode.get("delay_cancelled"))
    if key.startswith("hold-recovery:"):
        return key.split(":")[1] not in state["holds"]
    return True


def deliver(state, ledger, discord, *, event_check=None, ci_check=None,
            hold_recovery_check=None, sync_check=None):
    errors = 0
    for key, item in state["outbox"].items():
        if item["hash"] != dt.digest(item["payload"]):
            raise dt.Error("Outbox integrity mismatch")
        if item["status"] != "ready":
            if item["status"] in ("pending", "rejected"):
                print("::warning::Discord delivery requires PM inspection: " + dt.digest(key)[:12])
                errors += 1
            continue
        if not notice_active(state, key):
            invalidate(state, key)
            ledger.save(state)
            continue
        is_sync_notice = key.startswith(("hold:", "hold-delay:", "failure:sync:",
                                         "recovery:failure:sync:"))
        if sync_check and is_sync_notice:
            current = sync_check(key)
            if current is False:
                invalidate(state, key)
                ledger.save(state)
                continue
            if current is None:
                continue  # Unknown latest sync evidence preserves the ready entry.
        if ci_check and key.startswith(("failure:ci:", "recovery:failure:ci:")):
            current = ci_check(key)
            if current is False:
                invalidate(state, key)
                ledger.save(state)
                continue
            if current is None:
                continue  # Incomplete/pending current CI evidence cannot authorize POST.
        deps = item["dependencies"]
        if deps and not any(state["outbox"].get(d, {}).get("status") == "delivered" for d in deps):
            continue
        if key.startswith("hold-recovery:"):
            if hold_recovery_check is None:
                continue  # Recovery evidence is mandatory at the delivery boundary.
            current = hold_recovery_check(key)
            if current is None:
                continue  # A pending/failed/invalid latest sync cannot confirm recovery.
            if current is False:
                invalidate(state, key)
                ledger.save(state)
                continue
        if event_check and re.fullmatch(r"pr:[1-9][0-9]*:timeline:[1-9][0-9]*", key):
            current_message = event_check(key)
            if current_message is None:
                item["status"] = "retired"
                ledger.save(state)
                continue
            item["payload"] = seal_message(key, current_message)
            item["hash"] = dt.digest(item["payload"])
        item["status"] = "pending"
        ledger.save(state)  # A crash or ambiguous POST is fenced before sending.
        dynamic_notice = (
            bool(event_check and re.fullmatch(r"pr:[1-9][0-9]*:timeline:[1-9][0-9]*", key)) or
            bool(ci_check and key.startswith(("failure:ci:", "recovery:failure:ci:"))) or
            bool(key.startswith("hold-recovery:") and hold_recovery_check) or
            bool(sync_check and is_sync_notice)
        )

        def revalidate_dynamic_notice():
            if not notice_active(state, key):
                return False
            try:
                if event_check and re.fullmatch(r"pr:[1-9][0-9]*:timeline:[1-9][0-9]*", key):
                    return event_check(key) is not None
                if ci_check and key.startswith(("failure:ci:", "recovery:failure:ci:")):
                    return ci_check(key)
                if key.startswith("hold-recovery:") and hold_recovery_check:
                    return hold_recovery_check(key)
                if sync_check and is_sync_notice:
                    return sync_check(key)
            except Exception:
                return None
            return True

        previous_retry_guard = getattr(discord, "_before_rate_limit_retry", None)
        if dynamic_notice:
            discord._before_rate_limit_retry = revalidate_dynamic_notice
        try:
            mid = discord.send(item["payload"])
            if any(other_key != key and other.get("message_id") == mid for other_key, other in state["outbox"].items()):
                raise dt.Error("Discord message ID already confirms another event")
        except dt.RateLimitRejected:
            current = revalidate_dynamic_notice() if dynamic_notice else None
            # The HTTP 429 proves rejection. Do not retry now without a safe
            # retry time; keep it ready for a later fresh reconciliation.
            item["status"] = "ready"
            if current is False:
                if key.startswith("pr:"):
                    item["status"] = "retired"
                else:
                    invalidate(state, key)
            ledger.save(state)
            continue
        except dt.HTTPError as error:
            if error.status == 429 and dynamic_notice:
                current = revalidate_dynamic_notice()
                # A 429 is a confirmed rejection, so this attempt is no
                # longer protected by the ambiguous-POST pending fence.
                item["status"] = "ready"
                if current is False:
                    if key.startswith("pr:"):
                        item["status"] = "retired"
                    else:
                        invalidate(state, key)
                else:
                    # 429 confirms this attempt was rejected. It is safe to
                    # retry only on a later reconciliation after fresh checks.
                    item["status"] = "ready"
            else:
                item["status"] = "rejected"
            ledger.save(state)
            if item["status"] == "rejected":
                errors += 1
                print("::warning::Discord rejected delivery: " + dt.digest(key)[:12])
            continue
        except dt.Error:
            errors += 1
            print("::warning::Discord delivery outcome unknown: " + dt.digest(key)[:12])
            continue
        finally:
            if dynamic_notice:
                if previous_retry_guard is None:
                    try:
                        del discord._before_rate_limit_retry
                    except AttributeError:
                        pass
                else:
                    discord._before_rate_limit_retry = previous_retry_guard
        item.update(status="delivered", message_id=mid)
        ledger.save(state)  # Failed save keeps the remote pending fence; abort run.
    return errors


def resolve(state, key, outcome, mid, discord):
    if key not in state["outbox"]:
        raise dt.Error("Unknown delivery key")
    item = state["outbox"][key]
    if item["status"] not in ("pending", "rejected"):
        raise dt.Error("Delivery does not require recovery")
    if outcome == "found":
        if any(other_key != key and other.get("message_id") == mid for other_key, other in state["outbox"].items()):
            raise dt.Error("Discord message ID already confirms another event")
        item.update(status="delivered", message_id=discord.find(mid, item["payload"]))
    elif outcome == "not_sent":
        item.update(status="ready", message_id=None)
    else:
        raise dt.Error("Delivery remains unknown")


def project_backlog(token, objects):
    if not token:
        return set()
    try:
        reader = dt.GitHub(token)
        _, headers = reader.call("/user")
        scopes = {x.strip() for x in headers.get("X-OAuth-Scopes", headers.get("x-oauth-scopes", "")).split(",")}
        if "read:project" not in scopes or "project" in scopes:
            return set()
        import github_project as gp
        graph = gp.GraphQLClient(token)
        # Existing verifier binds owner/project/status IDs, using reads only.
        repository_node = reader.repo("")["node_id"]
        project = gp.fetch_project(graph, project_id=gp.EXPECTED_PROJECT_ID,
                                   owner_id=gp.EXPECTED_PROJECT_OWNER_ID,
                                   status_field_id="PVTSSF_lAHOBda_2c4BmKKVzhk0RA4",
                                   status_options=gp.EXPECTED_STATUS_OPTIONS,
                                   repository_node_id=repository_node)
        return {i["number"] for i in objects if "head" not in i and
                (project["items"].get(i["id"]) or {}).get("status_option_id") == gp.EXPECTED_STATUS_OPTIONS["백로그"]}
    except Exception:
        return set()  # Optional observation must never claim unverified registration.


def collect_objects(gh):
    issues = [x for x in gh.pages("/issues?state=all") if "pull_request" not in x]
    # List PR response omits some review/merge facts; get authoritative full objects.
    pulls = [gh.repo(f"/pulls/{x['number']}") for x in gh.pages("/pulls?state=all")]
    for pull in pulls:
        if not isinstance(pull, dict) or "head" not in pull or "base" not in pull:
            raise dt.Error("Incomplete current PR response")
        current_reviewers(pull)
        merge_fact(pull)
    objects = issues + pulls
    timelines = {x["number"]: gh.pages(f"/issues/{x['number']}/timeline") for x in objects}
    return objects, pulls, timelines


def current_pr_event(gh, key, mapping):
    number = int(key.split(":")[1])
    pull = gh.repo(f"/pulls/{number}")
    if not isinstance(pull, dict) or "head" not in pull or "base" not in pull or pull.get("number") != number:
        raise dt.Error("Incomplete current PR response")
    timeline = gh.pages(f"/issues/{number}/timeline")
    events = source_events([pull], {number: timeline}, mapping)
    return events.get(key)


def current_ci_notice(gh, key, workflows):
    """Revalidate delayed CI notices against current head and latest run before POST."""
    recovery = key.startswith("recovery:")
    parts = key.split(":at:")[0].removeprefix("recovery:").split(":")
    workflow = next((w for w in workflows if str(w["id"]) == parts[2]), None)
    if workflow is None:
        return False
    pull = gh.repo(f"/pulls/{parts[4]}") if parts[3] == "pr" else None
    if pull is not None and pull.get("state") == "closed":
        return False
    main_sha = gh.repo("/git/ref/heads/main")["object"]["sha"]
    if not valid_sha(main_sha):
        return None
    original = gh.repo(f"/actions/runs/{parts[-2]}")
    if not valid_sha(original.get("head_sha")) or pull is not None and not valid_sha((pull.get("head") or {}).get("sha")):
        return None
    verdict = ci_verdict(original, pull, workflow, main_sha, historical=recovery)
    if verdict is not True:
        return verdict
    if (type(original.get("id")) is not int or type(original.get("run_attempt")) is not int or
            original.get("id") != int(parts[-2]) or original.get("run_attempt") != int(parts[-1]) or
            original.get("status") != "completed" or original.get("conclusion") not in FAILURES):
        return None
    observed = gh.pages(f"/actions/workflows/{workflow['id']}/runs", "workflow_runs")
    listed_original = [r for r in observed if r.get("id") == original["id"]]
    if (len(listed_original) != 1 or
            ci_verdict(listed_original[0], pull, workflow, main_sha, historical=recovery) is not True or
            ci_run_binding(original) is None or
            ci_run_binding(original) != ci_run_binding(listed_original[0])):
        return None
    eligible = [r for r in observed if ci_matches(r, pull, workflow, main_sha)]
    if not eligible:
        return None
    candidate = max(eligible, key=run_order)
    if not _ci_counter_tail_verified(gh, observed, run_order(candidate), pull, workflow, main_sha):
        return None
    if any(ci_verdict(r, pull, workflow, main_sha) is None and run_order(r) >= run_order(candidate)
           for r in observed):
        return None
    latest = gh.repo(f"/actions/runs/{candidate['id']}")
    if ci_run_binding(candidate) is None or ci_run_binding(candidate) != ci_run_binding(latest):
        return None
    if recovery:
        if ":at:" not in key:
            return None
        bound_id, bound_attempt = (int(x) for x in key.split(":at:")[1].split(":"))
        boundary = gh.repo(f"/actions/runs/{bound_id}")
        listed_boundary = [r for r in observed if r.get("id") == bound_id]
        if (len(listed_boundary) != 1 or
                ci_verdict(listed_boundary[0], pull, workflow, main_sha, historical=True) is not True or
                ci_run_binding(boundary) is None or
                ci_run_binding(boundary) != ci_run_binding(listed_boundary[0])):
            return None
        if (boundary.get("id") != bound_id or ci_verdict(boundary, pull, workflow, main_sha, historical=True) is not True or
                type(boundary.get("run_number")) is not int or boundary["run_number"] <= 0):
            return None
        if (boundary.get("run_attempt") != bound_attempt or boundary.get("status") != "completed" or
                boundary.get("conclusion") != "success"):
            return None
        bound_order = (boundary["run_number"], bound_id, bound_attempt)
        latest_order = run_order(candidate)
        origin_order = run_order(original)
        if origin_order[0] > bound_order[0]:
            return None
        counter_rows = ci_recovery_counter_rows(observed, workflow, origin_order, latest_order)
        if counter_rows is None:
            return None
        if (origin_order[0] not in counter_rows or
                counter_rows[origin_order[0]].get(origin_order[2], (None,))[0] != origin_order[1]):
            return None
        # Verify the whole origin-to-latest interval. Failures before the first
        # clear belong to the same unresolved incident; a later recurrence
        # invalidates recovery anchored to the old failure.
        cleared = False
        for number in range(origin_order[0] + 1, latest_order[0] + 1):
            attempts = counter_rows[number]
            for run_id, prior in attempts.values():
                association = ci_verdict(prior, pull, workflow, main_sha, historical=True)
                if association is None:
                    return None
                detail = gh.repo(f"/actions/runs/{run_id}")
                detail_association = ci_verdict(detail, pull, workflow, main_sha, historical=True)
                if (ci_run_binding(detail) is None or ci_run_binding(detail) != ci_run_binding(prior) or
                        detail_association is not association):
                    return None
                if association is False:
                    continue  # The unrelated classification is bound to matching detail.
                if prior.get("run_attempt") != 1:
                    return None  # The workflow list may hide an earlier hold/failure attempt.
                if ci_verdict(detail, pull, workflow, main_sha, historical=True) is not True or detail.get("status") != "completed":
                    return None
                if detail.get("conclusion") in FAILURES:
                    if cleared:
                        return False
                    continue
                if detail.get("conclusion") not in {"success", "cancelled", "neutral", "skipped"}:
                    return None
                if detail.get("conclusion") == "success":
                    cleared = True
    if main_sha != gh.repo("/git/ref/heads/main")["object"]["sha"]:
        return None
    if pull is not None:
        pull = gh.repo(f"/pulls/{parts[4]}")
        if pull.get("state") == "closed":
            return False
        if not valid_sha((pull.get("head") or {}).get("sha")):
            return None
        verdict = ci_verdict(original, pull, workflow, main_sha, historical=recovery)
        if verdict is not True:
            return verdict
    if not ci_matches(latest, pull, workflow, main_sha) or latest.get("status") != "completed":
        return None
    if latest.get("conclusion") not in FAILURES | {"success"}:
        return None
    return (latest["conclusion"] == "success") if recovery else (latest["conclusion"] in FAILURES)


def refresh_ci_runs(gh, runs, pulls, workflows, main_sha):
    """A conflicting detail read must not change incident state before POST checks."""
    candidates = {}
    for workflow in workflows:
        for pull in [None] + pulls:
            eligible = [r for r in runs if ci_matches(r, pull, workflow, main_sha)]
            if not eligible:
                continue
            latest = max(eligible, key=run_order)
            if not _ci_counter_tail_verified(gh, runs, run_order(latest), pull, workflow, main_sha):
                raise dt.Error("Excluded CI run detail is unverified")
            candidates[latest["id"]] = latest
    refreshed = {}
    fields = ("id", "run_attempt", "run_number", "head_sha", "status", "conclusion",
              "workflow_id", "path", "event", "head_branch", "repository", "head_repository")
    for rid, candidate in candidates.items():
        detail = gh.repo(f"/actions/runs/{rid}")
        compared = fields + (("pull_requests",) if candidate.get("event") == "pull_request" else ())
        if any(k not in detail or k not in candidate or detail[k] != candidate[k] for k in compared):
            refreshed[rid] = {**candidate, "status": "in_progress", "conclusion": None}
        else:
            refreshed[rid] = detail
    return [refreshed.get(r["id"], r) for r in runs]


def sync_scope(run, *, workflow_endpoint=False):
    """Classify sync identity as related, incomplete, or definitely unrelated."""
    if not isinstance(run, dict):
        return False
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    head_repository_id = head_repository.get("id") if isinstance(head_repository, dict) else None
    workflow_id, path = run.get("workflow_id"), run.get("path")
    event = run.get("event")
    allowed_events = {"schedule", "issues", "pull_request_target", "workflow_dispatch"}
    # Only well-typed positive mismatches prove that a run is unrelated.
    # Missing or malformed identity fields are uncertainty, never proof.
    if ((type(workflow_id) is int and workflow_id != SYNC_ID) or
            (isinstance(path, str) and path != SYNC_PATH) or
            (type(repository_id) is int and repository_id != REPO_ID) or
            (type(head_repository_id) is int and head_repository_id != REPO_ID) or
            (isinstance(run.get("head_branch"), str) and run["head_branch"] != "main") or
            (isinstance(event, str) and event not in allowed_events)):
        return False
    positive_match = ((type(workflow_id) is int and workflow_id == SYNC_ID) or
                      path == SYNC_PATH or
                      (type(repository_id) is int and repository_id == REPO_ID) or
                      (type(head_repository_id) is int and head_repository_id == REPO_ID))
    complete_identity = (type(workflow_id) is int and workflow_id == SYNC_ID and path == SYNC_PATH and
                         type(repository_id) is int and repository_id == REPO_ID and
                         type(head_repository_id) is int and head_repository_id == REPO_ID and
                         run.get("head_branch") == "main" and event in allowed_events)
    if complete_identity:
        return True
    # The workflow-specific Actions endpoint itself is positive association
    # evidence. If every per-run identity field is absent, treat the row as an
    # incomplete candidate rather than silently skipping an observation gap.
    return None if positive_match or workflow_endpoint else False


def sync_context(gh, run, main_sha):
    if sync_scope(run) is not True:
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")):
        return False
    if run["head_sha"] == main_sha:
        return True
    compare = gh.repo(f"/compare/{run['head_sha']}...{main_sha}")
    return (compare.get("status") in ("ahead", "identical") and
            (compare.get("base_commit") or {}).get("sha") == run["head_sha"])


def trusted_sync(gh, run, main_sha):
    if not sync_context(gh, run, main_sha):
        return False
    return run["event"] != "workflow_dispatch" or (
        type((run.get("actor") or {}).get("id")) is int and
        (run.get("actor") or {}).get("id") == int(PM_ID) and
        type((run.get("triggering_actor") or {}).get("id")) is int and
        (run.get("triggering_actor") or {}).get("id") == int(PM_ID) and
        run["run_attempt"] == 1)


def latest_sync_holds(gh, main_sha):
    """Return the latest trusted successful full snapshot's held issue IDs."""
    try:
        if not valid_sha(main_sha):
            return None
        runs = gh.pages(f"/actions/workflows/{SYNC_ID}/runs", "workflow_runs")
        related = [(run, sync_scope(run, workflow_endpoint=True)) for run in runs]
        related = [(run, scope) for run, scope in related if scope is not False]
        if not related:
            return None
        latest, scope = max(related, key=lambda item: run_order(item[0]))
        if not _sync_counter_tail_verified(gh, runs, run_order(latest)):
            return None
        if (scope is not True or not valid_sha(latest.get("head_sha")) or
                latest.get("status") != "completed" or latest.get("conclusion") != "success" or
                not sync_context(gh, latest, main_sha) or
                not trusted_sync(gh, latest, main_sha)):
            return None
        report = dt.artifact_report(gh, latest)
        if (not report or not report.get("scan_complete") or report.get("dry_run") or
                report.get("kind") not in ("complete", "partial")):
            return None
        return {str(item["issue_number"]) for item in report["holds"]}
    except Exception:
        return None


def current_hold_recovery(gh, key, observed_main_sha):
    """Confirm recovery against fresh latest sync evidence and unchanged main."""
    try:
        match = re.fullmatch(r"hold-recovery:([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*)", key)
        if not match or not valid_sha(observed_main_sha):
            return None
        ref = gh.repo("/git/ref/heads/main")
        current_main = (ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None
        if not valid_sha(current_main) or current_main != observed_main_sha:
            return None
        verdict = sync_recovery_sequence(gh, match.group(1), int(match.group(2)),
                                         int(match.group(3)), current_main)
        after = gh.repo("/git/ref/heads/main")
        after_main = (after.get("object") or {}).get("sha") if isinstance(after, dict) else None
        if not valid_sha(after_main) or after_main != current_main or verdict is None:
            return None
        return verdict
    except Exception:
        return None


def current_sync_notice(gh, key, observed_main_sha, state):
    """Recheck sync-notice evidence immediately before each Discord POST."""
    try:
        kind, issue_number, reason_code = None, None, None
        if match := re.fullmatch(r"hold:([1-9][0-9]*):([A-Z][A-Z0-9_]{0,79}):([1-9][0-9]*)", key):
            kind, issue_number, reason_code = "hold", match.group(1), match.group(2)
            origin_id = int(match.group(3))
            origin_attempt = None
        elif match := re.fullmatch(r"hold-delay:([1-9][0-9]*):([A-Z][A-Z0-9_]{0,79}):.+", key):
            kind, issue_number, reason_code = "delay", match.group(1), match.group(2)
            reason = state.get("holds", {}).get(issue_number, {}).get("reasons", {}).get(reason_code)
            episode = (reason or {}).get("episode") or {}
            origin_id = episode.get("timer_origin_run_id")
            origin_attempt = episode.get("last_observation_attempt")
            if (episode.get("delay_alert_key") != key or episode.get("delay_cancelled") or
                    episode.get("last_observation_run_id") != origin_id):
                return False
            if type(origin_id) is not int or type(origin_attempt) is not int:
                return None
        elif match := re.fullmatch(r"failure:sync:([1-9][0-9]*):([1-9][0-9]*)", key):
            kind, origin_id, origin_attempt = "failure", int(match.group(1)), int(match.group(2))
        elif match := re.fullmatch(
                r"recovery:failure:sync:([1-9][0-9]*):([1-9][0-9]*):at:([1-9][0-9]*):([1-9][0-9]*)", key):
            kind, origin_id, origin_attempt = "failure-recovery", int(match.group(1)), int(match.group(2))
            recovery_id, recovery_attempt = int(match.group(3)), int(match.group(4))
        else:
            return False

        if not valid_sha(observed_main_sha):
            return None
        main_before = gh.repo("/git/ref/heads/main")
        main_before = (main_before.get("object") or {}).get("sha") if isinstance(main_before, dict) else None
        if not valid_sha(main_before) or main_before != observed_main_sha:
            return None
        runs = gh.pages(f"/actions/workflows/{SYNC_ID}/runs", "workflow_runs")
        if not isinstance(runs, list):
            return None

        related, counter_rows, run_numbers_by_id = [], {}, {}
        for run in runs:
            if not isinstance(run, dict):
                return None
            scope = sync_scope(run, workflow_endpoint=True)
            try:
                order = run_order(run)
            except (KeyError, TypeError, dt.Error):
                if scope is False:
                    continue
                return None
            if sync_counter_bound(run):
                run_number, run_id, attempt = order
                previous_number = run_numbers_by_id.get(run_id)
                if previous_number is not None and previous_number != run_number:
                    return None
                run_numbers_by_id[run_id] = run_number
                attempts = counter_rows.setdefault(run_number, {})
                existing_id = next(iter(attempts.values()))[0] if attempts else None
                if existing_id is not None and existing_id != run_id:
                    return None
                if attempt in attempts:
                    prior = attempts[attempt][1]
                    fields = ("workflow_id", "path", "repository", "head_repository", "head_sha",
                              "status", "conclusion", "head_branch", "event", "actor",
                              "triggering_actor")
                    if any(prior.get(field) != run.get(field) for field in fields):
                        return None
                    continue
                attempts[attempt] = (run_id, run)
            if scope is not False:
                related.append((run, scope, order))
        if not related:
            return None
        related.sort(key=lambda entry: entry[2])
        if not _sync_counter_tail_verified(gh, runs, related[-1][2]):
            return None
        origins = [(index, run, order) for index, (run, _, order) in enumerate(related)
                   if run.get("id") == origin_id and
                   (origin_attempt is None or run.get("run_attempt") == origin_attempt)]
        if len(origins) != 1:
            return None
        origin_index, origin, origin_order = origins[0]
        proof_start_index = origin_index
        if kind == "hold":
            reason = state.get("holds", {}).get(issue_number, {}).get("reasons", {}).get(reason_code, {})
            episode = reason.get("episode") or {}
            first_id, first_attempt = (episode.get("first_observed_run_id"),
                                       episode.get("first_observed_attempt"))
            first = [(index, run, order) for index, (run, _, order) in enumerate(related)
                     if run.get("id") == first_id and run.get("run_attempt") == first_attempt]
            if (type(first_id) is not int or type(first_attempt) is not int or len(first) != 1 or
                    first[0][0] >= origin_index or first[0][2][0] >= origin_order[0]):
                return None
            proof_start_index = first[0][0]
        proof_start_order = related[proof_start_index][2]
        latest_order = related[-1][2]
        if any(number not in counter_rows
               for number in range(proof_start_order[0], latest_order[0] + 1)):
            return None
        # Successful snapshots are identity-bound by artifact_report(). Other
        # statuses have no artifact, so bind their list classification directly
        # to the run detail before treating them as current evidence.
        for run, scope, order in related[proof_start_index:]:
            status = sync_run_status(run)
            if status == "completed" and run.get("conclusion") == "success":
                continue
            detail = verified_sync_run_detail(gh, run)
            if detail is None or sync_scope(detail, workflow_endpoint=True) is not scope:
                return None
        # Same-workflow runs on another event/branch still prove counter slots.
        # Verify that their unrelated classification survives the detail read.
        for number in range(proof_start_order[0], latest_order[0] + 1):
            for run_id, listed in counter_rows[number].values():
                if sync_scope(listed, workflow_endpoint=True) is not False:
                    continue
                detail = verified_sync_run_detail(gh, listed)
                if detail is None or sync_scope(detail, workflow_endpoint=True) is not False:
                    return None
        for run, scope, order in related[proof_start_index:]:
            if scope is not True:
                return None
            if order[0] > origin_order[0] and order[2] != 1:
                return None
            status = sync_run_status(run)
            if status is None:
                return None
            if (not valid_sha(run.get("head_sha")) or
                    not sync_context(gh, run, observed_main_sha) or
                    not trusted_sync(gh, run, observed_main_sha)):
                return None

        snapshots = []
        for run, _, order in related[proof_start_index:]:
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                continue
            report = dt.artifact_report(gh, run)
            if (not report or not report.get("scan_complete") or report.get("dry_run") or
                    report.get("kind") not in ("complete", "partial")):
                return None
            held = {(str(item["issue_number"]), item["reason_code"])
                    for item in report["holds"]}
            snapshots.append((order, held))
        origin_status = sync_run_status(origin)
        origin_report = next((held for order, held in snapshots if order == origin_order), None)

        if kind in ("hold", "delay"):
            target = (issue_number, reason_code)
            if origin_status != "completed" or origin.get("conclusion") != "success" or \
                    origin_report is None or target not in origin_report:
                return None
            if kind == "hold":
                first_order = related[proof_start_index][2]
                first_report = next((held for order, held in snapshots if order == first_order), None)
                confirming = [held for order, held in snapshots
                              if first_order < order <= origin_order]
                if (first_report is None or target not in first_report or len(confirming) < 1 or
                        any(target not in held for held in confirming)):
                    return None
            later = [held for order, held in snapshots if order > origin_order]
            if kind == "delay":
                verdict = not later
            else:
                # A later clear ends this alert's episode permanently. A
                # subsequent recurrence needs its own two-run alert and must
                # not revive an older ready/delivered notice.
                verdict = all(target in held for held in later)
        elif kind == "failure":
            if (origin_status != "completed" or origin.get("conclusion") not in FAILURES or
                    origin_order[2] != origin_attempt):
                return None
            verdict = not any(order > origin_order for order, _ in snapshots)
        else:
            recovery_order = next((order for run, _, order in related
                                   if run.get("id") == recovery_id and
                                   run.get("run_attempt") == recovery_attempt), None)
            if recovery_order is None:
                return None
            recovery_snapshot = next((held for order, held in snapshots
                                      if order == recovery_order), None)
            latest_run, latest_scope, latest_order = related[-1]
            if latest_scope is not True or latest_order[0] < recovery_order[0]:
                return None
            if (origin_status != "completed" or origin.get("conclusion") not in FAILURES or
                    origin_order[2] != origin_attempt or
                    recovery_snapshot is None or recovery_order[0] <= origin_order[0]):
                return None
            latest_status = sync_run_status(latest_run)
            if latest_status != "completed":
                return None
            cleared, recurrence = False, False
            for run, _, order in related[origin_index + 1:]:
                if sync_run_status(run) != "completed":
                    return None
                if run.get("conclusion") in FAILURES:
                    if cleared:
                        recurrence = True
                        break
                elif run.get("conclusion") == "success":
                    if not any(snapshot_order == order for snapshot_order, _ in snapshots):
                        return None
                    cleared = True
            if recurrence:
                verdict = False
            elif latest_run.get("conclusion") == "success":
                verdict = any(order == latest_order for order, _ in snapshots)
            else:
                return None

        main_after = gh.repo("/git/ref/heads/main")
        main_after = (main_after.get("object") or {}).get("sha") if isinstance(main_after, dict) else None
        if not valid_sha(main_after) or main_after != main_before:
            return None
        return verdict
    except Exception:
        return None


def sync_recovery_sequence(gh, issue_number, origin_run_id, origin_attempt, main_sha):
    """Validate a complete, attempt-safe counter interval before recovery."""
    runs = gh.pages(f"/actions/workflows/{SYNC_ID}/runs", "workflow_runs")
    related = []
    counter_rows = {}
    run_numbers_by_id = {}
    for run in runs:
        if not isinstance(run, dict):
            return None
        repository = run.get("repository")
        repository_id = repository.get("id") if isinstance(repository, dict) else None
        workflow_id, path = run.get("workflow_id"), run.get("path")
        explicitly_unrelated = (
            type(workflow_id) is int and workflow_id != SYNC_ID or
            isinstance(path, str) and path != SYNC_PATH or
            type(repository_id) is int and repository_id != REPO_ID
        )
        if explicitly_unrelated:
            continue
        try:
            order = run_order(run)
        except (KeyError, TypeError, dt.Error):
            return None

        # Only rows bound to this workflow and base repository can establish
        # continuity of its run_number counter. Other workflow/repository rows
        # never fill a hole, even when returned by the workflow-specific list.
        counter_bound = sync_counter_bound(run)
        if counter_bound:
            run_number, run_id, attempt = order
            known_number = run_numbers_by_id.get(run_id)
            if known_number is not None and known_number != run_number:
                return None
            run_numbers_by_id[run_id] = run_number
            attempts = counter_rows.setdefault(run_number, {})
            existing_id = next(iter(attempts.values()))[0] if attempts else None
            if existing_id is not None and existing_id != run_id:
                return None
            if attempt in attempts:
                prior = attempts[attempt][1]
                binding_fields = (
                    "workflow_id", "path", "repository", "head_repository", "head_sha",
                    "status", "conclusion", "head_branch", "event", "actor",
                    "triggering_actor",
                )
                if any(prior.get(field) != run.get(field) for field in binding_fields):
                    return None
                continue  # Identical pagination/replay duplicate.
            attempts[attempt] = (run_id, run)

        scope = sync_scope(run, workflow_endpoint=True)
        if scope is not False:
            related.append((run, scope, order))
    if not related:
        return None
    related.sort(key=lambda item: item[2])
    if not _sync_counter_tail_verified(gh, runs, related[-1][2]):
        return None
    origins = [i for i, (run, _, _) in enumerate(related)
               if run.get("id") == origin_run_id and run.get("run_attempt") == origin_attempt]
    if len(origins) != 1:
        return None
    origin_index = origins[0]
    origin_number = related[origin_index][2][0]
    if any(scope is None and order[0] >= origin_number
           for _, scope, order in related):
        return None
    origin_attempts = counter_rows.get(origin_number, {})
    if (not origin_attempts or max(origin_attempts) != origin_attempt or
            origin_attempts[origin_attempt][0] != origin_run_id):
        return None

    latest_number = related[-1][2][0]
    if any(number not in counter_rows for number in range(origin_number, latest_number + 1)):
        return None

    # The workflow-runs list can expose only a run's current attempt. A later
    # rerun may have replaced an earlier attempt that contained a valid hold;
    # without that historical evidence a clear cannot authorize recovery.
    for number in range(origin_number + 1, latest_number + 1):
        if max(counter_rows[number]) > 1:
            return None

    # Prove same-workflow counter rows that were classified unrelated from
    # their detail responses before allowing them to fill this interval.
    for number in range(origin_number, latest_number + 1):
        for _, listed in counter_rows[number].values():
            if sync_scope(listed, workflow_endpoint=True) is not False:
                continue
            detail = verified_sync_run_detail(gh, listed)
            if detail is None or sync_scope(detail, workflow_endpoint=True) is not False:
                return None

    for run, scope, order in related[origin_index:]:
        # Explicitly unrelated branch/event rows can fill a counter slot, but
        # their reports are never read or treated as sync snapshots.
        if scope is False:
            detail = verified_sync_run_detail(gh, run)
            if detail is None or sync_scope(detail, workflow_endpoint=True) is not False:
                return None
            continue
        if order[0] > origin_number and order[2] != 1:
            return None
        if (scope is not True or run.get("status") != "completed" or
                run.get("conclusion") != "success" or not valid_sha(run.get("head_sha")) or
                not sync_context(gh, run, main_sha) or not trusted_sync(gh, run, main_sha)):
            return None
        try:
            artifact = dt.artifact_report(gh, run)
        except (dt.Error, ValueError):
            return None
        if (not artifact or not artifact.get("scan_complete") or artifact.get("dry_run") or
                artifact.get("kind") not in ("complete", "partial")):
            return None
        if int(issue_number) in {item["issue_number"] for item in artifact["holds"]}:
            return False
    return True


def consume_sync_gap(state, run):
    """Advance a related completed run without trusting its report."""
    order = run_order(run)
    prior = tuple(state["sync_cursor"]) if state["sync_cursor"] else None
    if prior and order <= prior:
        return
    state["sync_cursor"] = list(order)
    reset_sync_streaks(state)


def reconcile_sync_runs(state, gh, runs, main_sha, mapping):
    related = [r for r in runs if sync_scope(r, workflow_endpoint=True) is not False]
    if related and not _sync_counter_tail_verified(gh, runs, run_order(max(related, key=run_order))):
        raise dt.Error("Excluded sync run detail is unverified")
    cursor = tuple(state["sync_cursor"]) if state["sync_cursor"] else None
    observed = sorted((r for r in runs if (cursor is None or run_order(r) > cursor) and
                       sync_scope(r, workflow_endpoint=True) is not False), key=run_order)
    if not observed:
        return
    latest = observed[-1]
    latest_status = sync_run_status(latest)
    # Startup observes one current candidate; an unknown status is consumed as
    # a gap so a malformed list row cannot poison every later poll.
    candidates = [latest] if cursor is None else observed
    if cursor is None and any(sync_run_status(run) != "completed" for run in observed[:-1]):
        # Do not move the cursor past a still-running earlier run. It may finish
        # after a newer run and must be consumed in run-number order next poll.
        reset_sync_streaks(state)
        return
    counter_numbers = set()
    for candidate in runs:
        if not isinstance(candidate, dict):
            continue
        try:
            number = run_order(candidate)[0]
        except (KeyError, TypeError, dt.Error):
            continue
        if sync_counter_bound(candidate):
            scope = sync_scope(candidate, workflow_endpoint=True)
            if scope is False:
                # A same-workflow unrelated row fills a counter slot only if
                # its exact detail response preserves that classification.
                detail = verified_sync_run_detail(gh, candidate)
                if detail is None or sync_scope(detail, workflow_endpoint=True) is not False:
                    continue
            elif scope is not True:
                continue
            counter_numbers.add(number)
    for run in candidates:
        order = run_order(run)
        if state["sync_cursor"] and order <= tuple(state["sync_cursor"]):
            continue
        status = sync_run_status(run)
        if status is None:
            consume_sync_gap(state, run)
            continue
        if status != "completed":
            # A related pending run is an observation gap. Reset only an
            # unannounced streak, then stop before later run numbers so a late
            # completion can still be replayed in order on the next poll.
            reset_sync_streaks(state)
            break
        if run.get("conclusion") in FAILURES and sync_scope(run, workflow_endpoint=True) is True:
            # A failed list row can create a durable incident and move the
            # cursor, so bind its identity/status/conclusion before any state
            # transition. On conflict, leave this candidate for the next poll.
            detail = verified_sync_run_detail(gh, run)
            if (detail is None or sync_scope(detail, workflow_endpoint=True) is not True or
                    detail.get("status") != "completed" or detail.get("conclusion") not in FAILURES):
                break
        prior_cursor = tuple(state["sync_cursor"]) if state["sync_cursor"] else None
        if prior_cursor and any(number not in counter_numbers
                                for number in range(prior_cursor[0] + 1, order[0])):
            reset_sync_streaks(state)
        if sync_scope(run, workflow_endpoint=True) is not True or not valid_sha(run.get("head_sha")):
            consume_sync_gap(state, run)
            continue
        report = None
        if trusted_sync(gh, run, main_sha):
            try:
                report = dt.artifact_report(gh, run)
            except (dt.Error, ValueError):
                pass
        # Every completed gap/failure is consumed, even while a newer run is pending.
        # Only the latest completed current snapshot can announce success/hold recovery.
        reconcile_sync(state, run, report, mapping,
                       current_snapshot=(order == run_order(latest) and latest_status == "completed"))


def workflows_config(raw, gh):
    data = dt.json_data(raw)
    if not isinstance(data, list) or len(data) > 20:
        raise dt.Error("Invalid CI workflow allowlist")
    result = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {"id", "path"} or type(item["id"]) is not int or item["id"] <= 0:
            raise dt.Error("Invalid CI workflow configuration")
        if item["id"] == SYNC_ID or item["path"] in {SYNC_PATH, NOTIFIER_PATH}:
            raise dt.Error("Reserved workflow cannot be CI")
        actual = gh.repo(f"/actions/workflows/{item['id']}")
        if actual.get("id") != item["id"] or actual.get("path") != item["path"] or actual.get("state") != "active":
            raise dt.Error("CI workflow identity mismatch")
        result.append({**item, "name": actual["name"]})
    if len({x["id"] for x in result}) != len(result):
        raise dt.Error("Duplicate CI workflow")
    return result


def manual_guard(env):
    return (env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_REF") == "refs/heads/main" and
            env.get("GITHUB_ACTOR_ID") == PM_ID and env.get("GITHUB_ACTOR") == env.get("GITHUB_TRIGGERING_ACTOR") and
            env.get("GITHUB_RUN_ATTEMPT") == "1" and env.get("DISCORD_APPROVED_SHA") == env.get("GITHUB_SHA") and
            bool(re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", ""))))


def diagnostic_reason(error):
    """Emit only fixed classifications, never exception text or response data."""
    if isinstance(error, dt.HTTPError):
        status = error.status
        return f"http_{status}" if type(status) is int and 100 <= status <= 599 else "http"
    fixed = {
        "Remote request outcome unknown": "request_unknown",
        "Response size limit exceeded": "response_limit",
        "Duplicate JSON key": "duplicate_json_key",
        "Incomplete paginated response": "pagination_count",
        "Source pagination incomplete": "pagination_incomplete",
        "Source pagination limit exceeded": "pagination_limit",
        "Main changed during observation": "main_changed",
        "Invalid API path": "api_path",
    }
    return fixed.get(str(error), "unclassified") if isinstance(error, dt.Error) else "unclassified"


def main(argv=None, env=None, *, github_factory=None, ledger_factory=None, discord_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=".github/discord-notifications.json")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    if env.get("DISCORD_NOTIFICATIONS_ENABLED") != "true":
        print("Discord 알림 비활성: 상태와 기준선을 변경하지 않습니다.")
        return 0
    stage = "configuration"
    try:
        config = dt.json_data(Path(args.config).read_bytes())
        if config != {"repository_id": REPO_ID, "sync_workflow_id": SYNC_ID, "ci_workflows": config.get("ci_workflows")}:
            raise dt.Error("Notification configuration mismatch")
        mapping = user_map(env.get("DISCORD_USER_MAP", "{}"))
        discord = (discord_factory or dt.Discord)(env.get("DISCORD_WEBHOOK_URL", ""), env.get("DISCORD_CHANNEL_ID", ""))
        stage = "destination_verify"
        discord.verify()  # Configuration/destination failures do not consume baseline/events.
        gh = (github_factory or dt.GitHub)(env.get("GITHUB_TOKEN", ""))
        ledger = (ledger_factory or dt.Ledger)(gh)
        workflows = workflows_config(json.dumps(config["ci_workflows"]), gh)
        stage = "ledger_load"
        state = ledger.load()
        bootstrap = state is None
        migration_needed = bool(state is not None and state.get("schema") == 1)
        if state is not None:
            upgrade_state(state)
        if bootstrap and not manual_guard(env):
            raise dt.Error("Initial baseline requires PM main dispatch")
        if env.get("DISCORD_RESOLVE_KEY"):
            if not manual_guard(env) or bootstrap:
                raise dt.Error("Delivery recovery requires PM main dispatch")
            resolve(state, env["DISCORD_RESOLVE_KEY"], env.get("DISCORD_RESOLVE_OUTCOME"),
                    env.get("DISCORD_RESOLVE_MESSAGE_ID"), discord)
            ledger.save(state)
        stage = "source_collect"
        objects, pulls, timelines = collect_objects(gh)
        backlog = project_backlog(env.get("DISCORD_PROJECT_READ_TOKEN"), objects)
        events = source_events(objects, timelines, mapping, backlog)
        if bootstrap:
            state = new_state()
            reconcile_events(state, events, bootstrap=True)
            stage = "baseline_save"
            ledger.save(state, bootstrap=True)
            # Durable activation boundary. A same-second unseen ID is still new.
            stage = "source_refresh"
            objects, pulls, timelines = collect_objects(gh)
            events = source_events(objects, timelines, mapping, backlog)
        before = copy.deepcopy(state)
        reconcile_events(state, events)
        main_sha = gh.repo("/git/ref/heads/main")["object"]["sha"]
        stage = "ci_observe"
        ci_runs = []
        for workflow in workflows:
            ci_runs.extend(gh.pages(f"/actions/workflows/{workflow['id']}/runs", "workflow_runs"))
        # Verify excluded counter tails before committing each stream's latest candidate.
        ci_runs = refresh_ci_runs(gh, ci_runs, pulls, workflows, main_sha)
        refreshed = [gh.repo(f"/pulls/{p['number']}") for p in pulls]
        if main_sha != gh.repo("/git/ref/heads/main")["object"]["sha"]:
            raise dt.Error("Main changed during observation")
        reconcile_ci(state, ci_runs, refreshed, workflows, main_sha, mapping)
        stage = "sync_observe"
        # Sync reconciliation can advance durable cursors even when an artifact
        # is untrusted. Stage it separately until main is confirmed unchanged
        # across list, detail, and artifact reads; this preserves unrelated CI
        # and outbox mutations if the sync observation must be discarded.
        try:
            sync_main_before = gh.repo("/git/ref/heads/main")["object"]["sha"]
            if not valid_sha(sync_main_before) or sync_main_before != main_sha:
                raise dt.Error("Main changed during observation")
            sync_runs = gh.pages(f"/actions/workflows/{SYNC_ID}/runs", "workflow_runs")
            sync_candidate = copy.deepcopy(state)
            reconcile_sync_runs(sync_candidate, gh, sync_runs, sync_main_before, mapping)
            sync_main_after = gh.repo("/git/ref/heads/main")["object"]["sha"]
            if not valid_sha(sync_main_after) or sync_main_after != sync_main_before:
                raise dt.Error("Main changed during observation")
            state = sync_candidate
        except Exception:
            print("::warning::동기화 실행 관측을 확인할 수 없어 다음 실행으로 미룹니다.")
        clock_mark = observation_clock.mark_snapshot()
        clock_reading = observation_clock.verify_snapshot(
            clock_mark, lambda: observation_clock.fetch_fresh_date(gh))
        reconcile_hold_delays(state, clock_reading, mapping)
        if state != before or migration_needed:
            stage = "ledger_save"
            ledger.save(state)
        stage = "delivery"
        def hold_recovery_check(key):
            return current_hold_recovery(gh, key, main_sha)

        def sync_check(key):
            return current_sync_notice(gh, key, main_sha, state)

        return 1 if deliver(state, ledger, discord,
                            event_check=lambda key: current_pr_event(gh, key, mapping),
                            ci_check=lambda key: current_ci_notice(gh, key, workflows),
                            hold_recovery_check=hold_recovery_check,
                            sync_check=sync_check) else 0
    except Exception as error:
        # Fixed message only: no exception repr, API response, token or webhook URL.
        print("::error::Discord 알림 처리 실패: 설정·실행 기록·전송 보류 상태를 확인하세요. "
              + f"stage={stage}; reason={diagnostic_reason(error)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
